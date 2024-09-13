import os
import gc
import wandb
import numpy as np
import argparse
import pandas as pd
from functools import partial

import sys
sys.path.append('pytorch-image-models')
import timm
import json
from model import Model
from data.data import CellCropsDataset
from data.utils import load_crops
from data.transform import train_transform, val_transform
from torch.utils.data import DataLoader, WeightedRandomSampler
import torch

from lee.e2e_lee import get_equivariance_metrics as get_lee_metrics
from lee.e2e_other import get_equivariance_metrics as get_discrete_metrics
from lee.loader import get_loaders, eval_average_metrics_wstd

def numparams(model):
    return sum(p.numel() for p in model.parameters())

def get_metrics(args, key, loader, model, max_mbs=400):
    discrete_metrics = eval_average_metrics_wstd(
        loader, partial(get_discrete_metrics, model), max_mbs=max_mbs,
    )
    lee_metrics = eval_average_metrics_wstd(
        loader, partial(get_lee_metrics, model), max_mbs=max_mbs,
    )
    metrics = pd.concat([lee_metrics, discrete_metrics], axis=1)

    metrics["dataset"] = key
    metrics["model"] = args.modelname
    metrics["params"] = numparams(model)

    return metrics

def get_args_parser():
    parser = argparse.ArgumentParser(description='Training Config', add_help=False)
    parser.add_argument('--output_dir', metavar='NAME', default='equivariance_metrics_cnns',help='experiment name')
    parser.add_argument('--modelname', metavar='NAME', default='resnet18', help='model name')
    parser.add_argument('--num_datapoints', type=int, default=60, help='use pretrained model')
    parser.add_argument('--base_path', type=str, help='configuration_path')
    return parser


def subsample_const_size(crops, size):
    """
    sample same number of cell from each class
    """
    final_crops = []
    crops = np.array(crops)
    labels = np.array([c._label for c in crops])
    unique_labels = np.unique(labels)
    class_sample_count = {t: len(np.where(labels == t)[0]) for t in unique_labels}
    print("class_sample_count before: ", class_sample_count)
    for lbl in np.unique(labels):
        indices = np.argwhere(labels == lbl).flatten()
        if (labels == lbl).sum() < size:
            chosen_indices = indices
        else:
            chosen_indices = np.random.choice(indices, size, replace=False)
        final_crops += crops[chosen_indices].tolist()
    return final_crops


def define_sampler(crops, hierarchy_match=None):
    """
    Sampler that sample from each cell category equally
    The hierarchy_match defines the cell category for each class.
    if None then each class will be category of it's own.
    """
    labels = np.array([c._label for c in crops])
    if hierarchy_match is not None:
        labels = np.array([hierarchy_match[str(l)] for l in labels])

    unique_labels = np.unique(labels)
    class_sample_count = {t: len(np.where(labels == t)[0]) for t in unique_labels}
    print("class sample count after: ", class_sample_count)
    weight = {k: sum(class_sample_count.values()) / v for k, v in class_sample_count.items()}
    samples_weight = np.array([weight[t] for t in labels])
    samples_weight = torch.from_numpy(samples_weight)
    return WeightedRandomSampler(samples_weight.double(), len(samples_weight))

def main(args):

    config_path = os.path.join(args.base_path, "config.json")
    with open(config_path) as f:
        config = json.load(f)

    train_crops, val_crops = load_crops(config["root_dir"],
                                        config["channels_path"],
                                        config["crop_size"],
                                        config["train_set"],
                                        config["val_set"],
                                        config["to_pad"],
                                        blacklist_channels=config["blacklist"])

    train_crops = np.array([c for c in train_crops if c._label >= 0])
    val_crops = np.array([c for c in val_crops if c._label >= 0])
    if "size_data" in config:
        train_crops = subsample_const_size(train_crops, config["size_data"])
        val_crops = subsample_const_size(val_crops, config["size_data"])
    sampler = define_sampler(train_crops, config["hierarchy_match"])
    shift = 5
    crop_input_size = config["crop_input_size"] if "crop_input_size" in config else 100
    aug = config["aug"] if "aug" in config else True
    training_transform = train_transform(crop_input_size, shift) if aug else val_transform(crop_input_size)
    train_dataset = CellCropsDataset(train_crops, transform=training_transform, mask=True)
    val_dataset = CellCropsDataset(val_crops, transform=val_transform(crop_input_size), mask=True)
    train_dataset_for_eval = CellCropsDataset(train_crops, transform=val_transform(crop_input_size), mask=True)
    device = "cuda"
    num_channels = sum(1 for line in open(config["channels_path"])) - len(config["blacklist"])
    class_num = config["num_classes"]

    model = Model(num_channels, class_num)

    model = model.to(device=device)

    train_loader = DataLoader(train_dataset, batch_size=config["batch_size"],
                              num_workers=config["num_workers"],
                              sampler=sampler if config["sample_batch"] else None,
                              shuffle=False if config["sample_batch"] else True)
    train_loader_for_eval = DataLoader(train_dataset_for_eval, batch_size=config["batch_size"],
                                       num_workers=config["num_workers"], shuffle=False)
    val_loader = DataLoader(val_dataset, batch_size=config["batch_size"],
                            num_workers=config["num_workers"], shuffle=False)
    print(len(train_loader), len(val_loader))

    wandb.init(project="LieDerivEquivariance", config=args)
    args.__dict__.update(wandb.config)

    print(args)

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    print(args.modelname)

    # model = getattr(timm.models, args.modelname)(pretrained=True)
    model.eval()

    evaluated_metrics = []

    # imagenet_train_loader, imagenet_test_loader = get_loaders(
    #     model,
    #     dataset="imagenet",
    #     data_dir="/imagenet",
    #     batch_size=1,
    #     num_train=args.num_datapoints,
    #     num_val=args.num_datapoints,
    #     args=args,
    #     train_split='train',
    #     val_split='validation',
    # )

    evaluated_metrics += [
        get_metrics(args, "Imagenet_train", train_loader, model),
        get_metrics(args, "Imagenet_test", val_loader, model)
    ]
    gc.collect()

    # _, cifar_test_loader = get_loaders(
    #     model,
    #     dataset="torch/cifar100",
    #     data_dir="/scratch/nvg7279/cifar",
    #     batch_size=1,
    #     num_train=args.num_datapoints,
    #     num_val=args.num_datapoints,
    #     args=args,
    #     train_split='train',
    #     val_split='validation',
    # )

    # evaluated_metrics += [get_metrics(args, "cifar100", cifar_test_loader, model, max_mbs=args.num_datapoints)]
    # gc.collect()

    # _, retinopathy_loader = get_loaders(
    #     model,
    #     dataset="tfds/diabetic_retinopathy_detection",
    #     data_dir="/scratch/nvg7279/tfds",
    #     batch_size=1,
    #     num_train=1e8,
    #     num_val=1e8,
    #     args=args,
    #     train_split="train",
    #     val_split="train",
    # )

    # evaluated_metrics += [get_metrics(args, "retinopathy", retinopathy_loader, model, max_mbs=args.num_datapoints)]
    # gc.collect()

    # _, histology_loader = get_loaders(
    #     model,
    #     dataset="tfds/colorectal_histology",
    #     data_dir="/scratch/nvg7279/tfds",
    #     batch_size=1,
    #     num_train=1e8,
    #     num_val=1e8,
    #     args=args,
    #     train_split="train",
    #     val_split="train",
    # )

    # evaluated_metrics += [get_metrics(args, "histology", histology_loader, model, max_mbs=args.num_datapoints)]
    # gc.collect()

    df = pd.concat(evaluated_metrics)
    df.to_csv(os.path.join(args.output_dir, args.modelname + ".csv"))

if __name__ == "__main__":
    args = get_args_parser().parse_args()
    main(args)
