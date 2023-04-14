from typing import Iterator

import torch
from diffusers.utils.import_utils import is_xformers_available
from torch import Tensor
from torch.nn import Parameter
from torch.optim import Optimizer

from modules.model.StableDiffusionModel import StableDiffusionModel
from modules.modelSetup.BaseModelSetup import BaseModelSetup
from modules.util import create
from modules.util.TrainProgress import TrainProgress
from modules.util.args.TrainArgs import TrainArgs


class StableDiffusionFineTuneSetup(BaseModelSetup):
    def __init__(
            self,
            train_device: torch.device,
            temp_device: torch.device,
            debug_mode: bool,
    ):
        super(StableDiffusionFineTuneSetup, self).__init__(
            train_device=train_device,
            temp_device=temp_device,
            debug_mode=debug_mode,
        )

    def create_parameters(
            self,
            model: StableDiffusionModel,
            args: TrainArgs,
    ) -> Iterator[Parameter]:
        if args.train_text_encoder:
            return list(model.text_encoder.parameters()) + list(model.unet.parameters())
        else:
            return list(model.unet.parameters())

    def setup_model(
            self,
            model: StableDiffusionModel,
            args: TrainArgs,
    ):
        train_text_encoder = args.train_text_encoder and (model.train_progress.epoch < args.train_text_encoder_epochs)

        model.text_encoder.requires_grad_(train_text_encoder)
        model.vae.requires_grad_(False)
        model.unet.requires_grad_(True)

        if model.optimizer_state_dict is not None and model.optimizer is None:
            model.optimizer = create.create_optimizer(self.create_parameters(model, args), args)
            # TODO: this will break if the optimizer class changed during a restart
            model.optimizer.load_state_dict(model.optimizer_state_dict)
            del model.optimizer_state_dict
        elif model.optimizer_state_dict is None and model.optimizer is None:
            model.optimizer = create.create_optimizer(self.create_parameters(model, args), args)

    def setup_eval_device(
            self,
            model: StableDiffusionModel
    ):
        model.text_encoder.to(self.train_device)
        model.vae.to(self.train_device)
        model.unet.to(self.train_device)
        if model.depth_estimator is not None:
            model.depth_estimator.to(self.train_device)

        model.text_encoder.eval()
        model.vae.eval()
        model.unet.eval()

    def setup_train_device(
            self,
            model: StableDiffusionModel,
            args: TrainArgs,
    ):
        model.text_encoder.to(self.train_device)
        model.vae.to(self.train_device if self.debug_mode else self.temp_device)
        model.unet.to(self.train_device)
        if model.depth_estimator is not None:
            model.depth_estimator.to(self.temp_device)

        if is_xformers_available():
            try:
                model.vae.enable_xformers_memory_efficient_attention()
                model.unet.enable_xformers_memory_efficient_attention()
            except Exception as e:
                print(
                    "Could not enable memory efficient attention. Make sure xformers is installed"
                    f" correctly and a GPU is available: {e}"
                )

        model.unet.enable_gradient_checkpointing()
        if args.train_text_encoder:
            model.text_encoder.gradient_checkpointing_enable()

        model.text_encoder.train()
        model.vae.train()
        model.unet.train()

    def create_optimizer(
            self,
            model: StableDiffusionModel,
            args: TrainArgs,
    ) -> Optimizer:
        return model.optimizer

    def get_train_progress(
            self,
            model: StableDiffusionModel,
            args: TrainArgs,
    ) -> TrainProgress:
        return model.train_progress

    def predict(
            self,
            model: StableDiffusionModel,
            batch: dict,
            args: TrainArgs,
            train_progress: TrainProgress
    ) -> (Tensor, Tensor):
        latent_image = batch['latent_image']
        scaled_latent_image = latent_image * model.vae.scaling_factor

        latent_conditioning_image = None
        scaled_latent_conditioning_image = None
        if args.model_type.has_conditioning_image_input():
            latent_conditioning_image = batch['latent_conditioning_image']
            scaled_latent_conditioning_image = latent_conditioning_image * model.vae.scaling_factor

        generator = torch.Generator(device=args.train_device)
        generator.manual_seed(train_progress.global_step)

        if args.offset_noise_weight > 0:
            normal_noise = torch.randn(scaled_latent_image.shape, generator=generator, device=args.train_device, dtype=args.train_dtype)
            offset_noise = torch.randn(scaled_latent_image.shape[0], scaled_latent_image.shape[1], 1, 1, generator=generator, device=args.train_device, dtype=args.train_dtype)
            latent_noise = normal_noise + (args.offset_noise_weight * offset_noise)
        else:
            latent_noise = torch.randn(scaled_latent_image.shape, generator=generator, device=args.train_device, dtype=args.train_dtype)

        timestep = torch.randint(
            low=0,
            high=int(model.noise_scheduler.config['num_train_timesteps'] * args.max_noising_strength),
            size=(scaled_latent_image.shape[0],),
            device=scaled_latent_image.device,
        ).long()

        scaled_noisy_latent_image = model.noise_scheduler.add_noise(original_samples=scaled_latent_image, noise=latent_noise, timesteps=timestep)

        text_encoder_output = model.text_encoder(batch['tokens'], return_dict=True)[0]

        if args.model_type.has_mask_input() and args.model_type.has_conditioning_image_input():
            latent_input = torch.concat([scaled_noisy_latent_image, batch['latent_mask'], scaled_latent_conditioning_image], 1)
        else:
            latent_input = scaled_noisy_latent_image

        if args.model_type.has_depth_input():
            predicted_latent_noise = model.unet(latent_input, timestep, text_encoder_output, batch['latent_depth']).sample
        else:
            predicted_latent_noise = model.unet(latent_input, timestep, text_encoder_output).sample

        if args.debug_mode:
            with torch.no_grad():
                # noise
                noise = model.vae.decode(latent_noise / model.vae.scaling_factor).sample
                noise = noise.clamp(-1, 1)
                self.save_image(noise, args.debug_dir + "/training_batches", "1-noise", train_progress.global_step)

                # predicted noise
                predicted_noise = model.vae.decode(predicted_latent_noise / model.vae.scaling_factor).sample
                predicted_noise = predicted_noise.clamp(-1, 1)
                self.save_image(predicted_noise, args.debug_dir + "/training_batches", "2-predicted_noise", train_progress.global_step)

                # noisy image
                noisy_latent_image = scaled_noisy_latent_image / model.vae.scaling_factor
                noisy_image = model.vae.decode(noisy_latent_image).sample
                noisy_image = noisy_image.clamp(-1, 1)
                self.save_image(noisy_image, args.debug_dir + "/training_batches", "3-noisy_image", train_progress.global_step)

                # predicted image
                sqrt_alpha_prod = model.noise_scheduler.alphas_cumprod[timestep] ** 0.5
                sqrt_alpha_prod = sqrt_alpha_prod.flatten().reshape(-1, 1, 1, 1)

                sqrt_one_minus_alpha_prod = (1 - model.noise_scheduler.alphas_cumprod[timestep]) ** 0.5
                sqrt_one_minus_alpha_prod = sqrt_one_minus_alpha_prod.flatten().reshape(-1, 1, 1, 1)

                scaled_predicted_latent_image = (scaled_noisy_latent_image - predicted_latent_noise * sqrt_one_minus_alpha_prod) / sqrt_alpha_prod
                predicted_latent_image = scaled_predicted_latent_image / model.vae.scaling_factor
                predicted_image = model.vae.decode(predicted_latent_image).sample
                predicted_image = predicted_image.clamp(-1, 1)
                self.save_image(predicted_image, args.debug_dir + "/training_batches", "4-predicted_image", model.train_progress.global_step)

                # image
                image = model.vae.decode(latent_image).sample
                image = image.clamp(-1, 1)
                self.save_image(image, args.debug_dir + "/training_batches", "5-image", model.train_progress.global_step)

                # conditioning image
                if args.model_type.has_conditioning_image_input():
                    conditioning_image = model.vae.decode(latent_conditioning_image).sample
                    conditioning_image = conditioning_image.clamp(-1, 1)
                    self.save_image(conditioning_image, args.debug_dir + "/training_batches", "6-conditioning_image", train_progress.global_step)

        return predicted_latent_noise, latent_noise