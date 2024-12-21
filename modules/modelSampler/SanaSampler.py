import inspect
import os
from collections.abc import Callable
from pathlib import Path

import torch
from PIL.Image import Image
from tqdm import tqdm

from modules.model.SanaModel import SanaModel
from modules.modelSampler.BaseModelSampler import BaseModelSampler
from modules.util.config.SampleConfig import SampleConfig
from modules.util.enum.ImageFormat import ImageFormat
from modules.util.enum.ModelType import ModelType
from modules.util.enum.NoiseScheduler import NoiseScheduler
from modules.util.torch_util import torch_gc


class SanaSampler(BaseModelSampler):
    def __init__(
            self,
            train_device: torch.device,
            temp_device: torch.device,
            model: SanaModel,
            model_type: ModelType,
    ):
        super().__init__(train_device, temp_device)

        self.model = model
        self.model_type = model_type
        self.pipeline = model.create_pipeline()

    @torch.no_grad()
    def __sample_base(
            self,
            prompt: str,
            negative_prompt: str,
            height: int,
            width: int,
            seed: int,
            random_seed: bool,
            diffusion_steps: int,
            cfg_scale: float,
            noise_scheduler: NoiseScheduler,
            cfg_rescale: float = 0.7,
            text_encoder_layer_skip: int = 0,
            force_last_timestep: bool = False,
            on_update_progress: Callable[[int, int], None] = lambda _, __: None,
    ) -> Image:
        with self.model.autocast_context:
            generator = torch.Generator(device=self.train_device)
            if random_seed:
                generator.seed()
            else:
                generator.manual_seed(seed)

            noise_scheduler = self.pipeline.scheduler
            image_processor = self.pipeline.image_processor
            transformer = self.pipeline.transformer
            vae = self.pipeline.vae
            vae_scale_factor = self.pipeline.vae_scale_factor

            # prepare prompt
            self.model.text_encoder_to(self.train_device)

            prompt_embedding, tokens_attention_mask = self.model.encode_text(
                text=prompt,
                train_device=self.train_device,
                batch_size=1,
                text_encoder_layer_skip=text_encoder_layer_skip,
            )

            negative_prompt_embedding, negative_tokens_attention_mask = self.model.encode_text(
                text=negative_prompt,
                train_device=self.train_device,
                batch_size=1,
                text_encoder_layer_skip=text_encoder_layer_skip,
            )

            combined_prompt_embedding = torch.cat([negative_prompt_embedding, prompt_embedding])
            combined_prompt_attention_mask = torch.cat([negative_tokens_attention_mask, tokens_attention_mask])

            self.model.text_encoder_to(self.temp_device)
            torch_gc()

            # prepare timesteps
            noise_scheduler.set_timesteps(diffusion_steps, device=self.train_device)
            timesteps = noise_scheduler.timesteps

            if force_last_timestep:
                last_timestep = torch.ones(1, device=self.train_device, dtype=torch.int64) \
                                * (noise_scheduler.config.num_train_timesteps - 1)

                # add the final timestep to force predicting with zero snr
                timesteps = torch.cat([last_timestep, timesteps])

            # prepare latent image
            num_channels_latents = transformer.config.in_channels
            latent_image = torch.randn(
                size=(1, num_channels_latents, height // vae_scale_factor, width // vae_scale_factor),
                generator=generator,
                device=self.train_device,
                dtype=torch.float32
            ) * noise_scheduler.init_noise_sigma

            # denoising loop
            extra_step_kwargs = {}
            if "generator" in set(inspect.signature(noise_scheduler.step).parameters.keys()):
                extra_step_kwargs["generator"] = generator

            # denoising loop
            self.model.transformer_to(self.train_device)
            for i, timestep in enumerate(tqdm(timesteps, desc="sampling")):
                latent_model_input = torch.cat([latent_image] * 2)

                # predict the noise residual
                noise_pred = transformer(
                    latent_model_input.to(dtype=self.model.train_dtype.torch_dtype()),
                    encoder_hidden_states=combined_prompt_embedding \
                        .to(dtype=self.model.train_dtype.torch_dtype()),
                    encoder_attention_mask=combined_prompt_attention_mask \
                        .to(dtype=self.model.train_dtype.torch_dtype()),
                    timestep=timestep.expand(latent_model_input.shape[0]),
                ).sample

                # cfg
                noise_pred_negative, noise_pred_positive = noise_pred.chunk(2)
                noise_pred = noise_pred_negative + cfg_scale * (noise_pred_positive - noise_pred_negative)

                if cfg_rescale > 0.0:
                    # From: Common Diffusion Noise Schedules and Sample Steps are Flawed (https://arxiv.org/abs/2305.08891)
                    std_positive = noise_pred_positive.std(dim=list(range(1, noise_pred_positive.ndim)), keepdim=True)
                    std_pred = noise_pred.std(dim=list(range(1, noise_pred.ndim)), keepdim=True)
                    noise_pred_rescaled = noise_pred * (std_positive / std_pred)
                    noise_pred = (
                            cfg_rescale * noise_pred_rescaled + (1 - cfg_rescale) * noise_pred
                    )

                # compute the previous noisy sample x_t -> x_t-1
                latent_image = noise_scheduler.step(
                    noise_pred, timestep, latent_image, return_dict=False, **extra_step_kwargs
                )[0]

                on_update_progress(i + 1, len(timesteps))

            self.model.transformer_to(self.temp_device)
            torch_gc()

            # decode
            self.model.vae_to(self.train_device)

            latent_image = latent_image.to(dtype=vae.dtype)
            image = vae.decode(latent_image / vae.config.scaling_factor, return_dict=False)[0]

            do_denormalize = [True] * image.shape[0]
            image = image_processor.postprocess(image, output_type='pil', do_denormalize=do_denormalize)

            self.model.vae_to(self.temp_device)

            return image[0]

    def sample(
            self,
            sample_config: SampleConfig,
            destination: str,
            image_format: ImageFormat,
            on_sample: Callable[[Image], None] = lambda _: None,
            on_update_progress: Callable[[int, int], None] = lambda _, __: None,
    ):
        prompt = self.model.add_embeddings_to_prompt(sample_config.prompt)
        negative_prompt = self.model.add_embeddings_to_prompt(sample_config.negative_prompt)

        image = self.__sample_base(
            prompt=prompt,
            negative_prompt=negative_prompt,
            height=self.quantize_resolution(sample_config.height, 32),
            width=self.quantize_resolution(sample_config.width, 32),
            seed=sample_config.seed,
            random_seed=sample_config.random_seed,
            diffusion_steps=sample_config.diffusion_steps,
            cfg_scale=sample_config.cfg_scale,
            noise_scheduler=sample_config.noise_scheduler,
            cfg_rescale=0.7 if sample_config.force_last_timestep else 0.0,
            text_encoder_layer_skip=sample_config.text_encoder_1_layer_skip,
            force_last_timestep=sample_config.force_last_timestep,
            on_update_progress=on_update_progress,
        )

        os.makedirs(Path(destination).parent.absolute(), exist_ok=True)
        image.save(destination)

        on_sample(image)