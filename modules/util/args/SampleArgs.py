import argparse

from modules.util.enum.ModelType import ModelType


class SampleArgs:
    model_type: ModelType
    base_model_name: str
    prompt: str
    destination: str

    def __init__(self, args: dict):
        for (key, value) in args.items():
            setattr(self, key, value)

    @staticmethod
    def parse_args() -> 'SampleArgs':
        parser = argparse.ArgumentParser(description="One Trainer Training Script.")

        parser.add_argument("--model-type", type=ModelType, required=True, dest="model_type", help="Type of the base model", choices=list(ModelType))
        parser.add_argument("--base-model-name", type=str, required=True, dest="base_model_name", help="The base model to start training from")
        parser.add_argument("--prompt", type=str, required=True, dest="prompt", help="The prompt for sampling")
        parser.add_argument("--destination", type=str, required=True, dest="destination", help="The destination to save the output")

        return SampleArgs(vars(parser.parse_args()))