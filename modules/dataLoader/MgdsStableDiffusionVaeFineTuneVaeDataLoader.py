import json

from mgds.DebugDataLoaderModules import DecodeVAE, SaveImage
from mgds.DiffusersDataLoaderModules import *
from mgds.GenericDataLoaderModules import *
from mgds.MGDS import MGDS, TrainDataLoader, OutputPipelineModule
from mgds.TransformersDataLoaderModules import *

from modules.model.StableDiffusionModel import StableDiffusionModel
from modules.util.TrainProgress import TrainProgress
from modules.util.args.TrainArgs import TrainArgs


class MgdsStableDiffusionFineTuneVaeDataLoader:
    def __init__(
            self,
            args: TrainArgs,
            model: StableDiffusionModel,
            train_progress: TrainProgress,
    ):
        with open(args.concept_file_name, 'r') as f:
            concepts = json.load(f)

        self.ds = create_dataset(
            args=args,
            model=model,
            concepts=concepts,
            train_progress=train_progress,
        )
        self.dl = TrainDataLoader(self.ds, args.batch_size)


def __enumerate_input_modules(args: TrainArgs) -> list:
    supported_extensions = ['.bmp', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp']

    collect_paths = CollectPaths(
        concept_in_name='concept', path_in_name='path', name_in_name='name', path_out_name='image_path', concept_out_name='concept', extensions=supported_extensions, include_postfix=None, exclude_postfix=['-masklabel']
    )

    mask_path = ModifyPath(in_name='image_path', out_name='mask_path', postfix='-masklabel', extension='.png')

    modules = [collect_paths]

    if args.masked_training:
        modules.append(mask_path)

    return modules


def __load_input_modules(args: TrainArgs) -> list:
    load_image = LoadImage(path_in_name='image_path', image_out_name='image', range_min=-1.0, range_max=1.0)
    load_mask = LoadImage(path_in_name='mask_path', image_out_name='mask', range_min=0, range_max=1, channels=1)

    modules = [load_image]

    if args.masked_training:
        modules.append(load_mask)

    return modules


def __mask_augmentation_modules(args: TrainArgs) -> list:
    inputs = ['image']

    random_mask_rotate_crop = RandomMaskRotateCrop(mask_name='mask', additional_names=inputs, min_size=args.resolution, min_padding_percent=10, max_padding_percent=30, max_rotate_angle=20)

    modules = []

    if args.masked_training and args.random_rotate_and_crop:
        modules.append(random_mask_rotate_crop)

    return modules


def __aspect_bucketing_in(args: TrainArgs):
    calc_aspect = CalcAspect(image_in_name='image', resolution_out_name='original_resolution')
    aspect_bucketing = AspectBucketing(
        batch_size=args.batch_size, target_resolution=args.resolution,
        resolution_in_name='original_resolution',
        scale_resolution_out_name='scale_resolution', crop_resolution_out_name='crop_resolution', possible_resolutions_out_name='possible_resolutions'
    )

    modules = []

    if args.aspect_ratio_bucketing:
        modules.append(calc_aspect)
        modules.append(aspect_bucketing)

    return modules


def __crop_modules(args: TrainArgs):
    scale_crop_image = ScaleCropImage(image_in_name='image', scale_resolution_in_name='scale_resolution', crop_resolution_in_name='crop_resolution', image_out_name='image')
    scale_crop_mask = ScaleCropImage(image_in_name='mask', scale_resolution_in_name='scale_resolution', crop_resolution_in_name='crop_resolution', image_out_name='mask')

    modules = [scale_crop_image]

    if args.masked_training:
        modules.append(scale_crop_mask)

    return modules


def __augmentation_modules(args: TrainArgs):
    inputs = ['image']

    if args.masked_training:
        inputs.append('mask')

    random_flip = RandomFlip(names=inputs)

    modules = [random_flip]

    return modules


def __preparation_modules(args: TrainArgs, model: StableDiffusionModel):
    image = EncodeVAE(in_name='image', out_name='latent_image_distribution', vae=model.vae)
    mask = Downscale(in_name='mask', out_name='latent_mask')

    modules = [image]

    if args.masked_training:
        modules.append(mask)

    return modules


def __cache_modules(args: TrainArgs):
    split_names = ['image', 'latent_image_distribution']

    if args.masked_training:
        split_names.append('latent_mask')

    aggregate_names = ['crop_resolution', 'image_path']

    disk_cache = DiskCache(cache_dir=args.cache_dir, split_names=split_names, aggregate_names=aggregate_names, cached_epochs=args.latent_caching_epochs)

    modules = []

    if args.latent_caching:
        modules.append(disk_cache)

    return modules


def __output_modules(args: TrainArgs):
    output_names = ['image', 'latent_image', 'image_path']

    if args.masked_training:
        output_names.append('latent_mask')

    image_sample = SampleVAEDistribution(in_name='latent_image_distribution', out_name='latent_image', mode='mean')
    batch_sorting = AspectBatchSorting(resolution_in_name='crop_resolution', names=output_names, batch_size=args.batch_size, sort_resolutions_for_each_epoch=True)
    output = OutputPipelineModule(names=output_names)

    modules = [image_sample, batch_sorting, output]

    return modules


def create_dataset(
        args: TrainArgs,
        model: StableDiffusionModel,
        concepts: list[dict],
        train_progress: TrainProgress,
):
    enumerate_input = __enumerate_input_modules(args)
    load_input = __load_input_modules(args)
    mask_augmentation = __mask_augmentation_modules(args)
    aspect_bucketing_in = __aspect_bucketing_in(args)
    crop_modules = __crop_modules(args)
    augmentation_modules = __augmentation_modules(args)
    preparation_modules = __preparation_modules(args, model)
    cache_modules = __cache_modules(args)
    output_modules = __output_modules(args)

    debug_dir = os.path.join(args.debug_dir, "dataloader")
    debug_modules = [
        DecodeVAE(in_name='latent_image', out_name='decoded_image', vae=model.vae),
        SaveImage(image_in_name='decoded_image', original_path_in_name='image_path', path=debug_dir, in_range_min=-1, in_range_max=1),
        SaveImage(image_in_name='mask', original_path_in_name='image_path', path=debug_dir, in_range_min=0, in_range_max=1),
        # SaveImage(image_in_name='latent_mask', original_path_in_name='image_path', path=debug_dir, in_range_min=0, in_range_max=1),
    ]

    settings = {}

    ds = MGDS(
        args.train_device,
        args.train_dtype,
        args.train_dtype != torch.float32,
        concepts,
        settings,
        [
            enumerate_input,
            load_input,
            mask_augmentation,
            aspect_bucketing_in,
            crop_modules,
            augmentation_modules,
            preparation_modules,
            cache_modules,
            output_modules,

            debug_modules if args.debug_mode else None,
        ],
        batch_size=args.batch_size,
        initial_epoch=train_progress.epoch,
        initial_epoch_sample=train_progress.epoch_sample,
    )

    return ds
