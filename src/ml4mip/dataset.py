import functools
import logging
import operator
import random
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from multiprocessing import Pool
from pathlib import Path

# from typing import override
import matplotlib.pyplot as plt
import nibabel as nib
import numpy as np
import torch
from hydra.core.config_store import ConfigStore
from monai.config import KeysCollection
from monai.data import MetaTensor
from monai.transforms import (
    Compose,
    LoadImage,
    LoadImaged,
    MapTransform,
    Randomizable,
    RandSpatialCropd,
    Resized,
    ResizeWithPadOrCrop,
    ResizeWithPadOrCropd,
    ScaleIntensityd,
    Spacing,
    Spacingd,
    ToTensord,
)
from scipy.stats import truncnorm
from torch.utils.data import Dataset

logger = logging.getLogger(__name__)


class TransformType(Enum):
    RESIZE = "resize"
    PATCH_CENTER_GAUSSIAN = "patch_center_gaussian"
    PATCH_POS_CENTER = "patch_pos"
    PATCH_UNIFORM = "patch_uniform"
    STD = "std"
    TOTENSOR = "totensor"


class MaskOperations(Enum):
    BINARY_CLASS = "binary"
    STD = "std"


# this was calculated after observing the maximum size of the images after the resampling
TARGET_PIXEL_DIM = (0.35, 0.35, 0.5)
# That way we don't need to add too much padding.
TARGET_SPATIAL_SIZE = (600, 600, 280)
# This was determined with some experiments. It's a good value for the sigma.
GOOD_SIGMA_RATIO = 0.1
POS_CENTER_PROB = 0.75
# this was calculated by the mean and std voxel value of the dataset
DATASET_VALUE_MEAN = -186.26184
DATASET_VALUE_STD = 440.80203


@dataclass
class DatasetConfig:
    # Don't change these values unless you know what you are doing:
    data_dir: str = "/data/training_data"  # path to the data in the directory
    mask_dir: str = "/data/training_data"  # path to the data in the directory
    image_affix: tuple[str] = ("", ".img.nii.gz")
    mask_affix: tuple[str] = ("", ".label.nii.gz")

    transform: TransformType = TransformType.PATCH_POS_CENTER
    size: tuple[int, int, int] = (96, 96, 96)
    train: bool = True
    split_ratio: float = 0.9
    target_pixel_dim: tuple[float, float, float] = TARGET_PIXEL_DIM
    target_spatial_size: tuple[int, int, int] = TARGET_SPATIAL_SIZE
    sigma_ratio: float = GOOD_SIGMA_RATIO
    pos_center_prob: float = POS_CENTER_PROB
    max_samples: int | None = None
    cache: bool = False
    cache_pooling: int = 0
    mask_operation: MaskOperations = MaskOperations.STD
    max_epochs: int = 1
    grouped: bool = False


@dataclass
class DataLoaderConfig:
    """This class is necessary to load data for training and validation."""

    train: DatasetConfig = field(default_factory=DatasetConfig)
    val: DatasetConfig = field(default_factory=DatasetConfig)


_cs = ConfigStore.instance()
_cs.store(
    group="dataset",
    name="base_dataset",
    node=DatasetConfig,
)

_cs.store(
    group="dataset",
    name="base_dataloader",
    node=DataLoaderConfig,
)


##### Datasets #####


class ABCNiftiDataset(Dataset, ABC):
    def __init__(
        self,
        data_dir: str | Path,
        mask_dir: str | Path | None = None,
        image_affix: tuple[str] = ("", ".img.nii.gz"),
        mask_affix: tuple[str] = ("", ".label.nii.gz"),
        transform: Callable | None = None,
        train: bool = True,
        split_ratio: float = 0.9,
        max_samples: int | None = None,
        cache: bool = False,
        cache_pooling: int = 0,
        mask_operation: MaskOperations = MaskOperations.STD,
    ) -> None:
        self.use_cache = cache

        self.data_dir: Path = Path(data_dir)
        self.mask_dir: Path = self.data_dir
        self.mask_operation = mask_operation
        self.cache_pooling = cache_pooling
        self.max_samples = max_samples
        self.split_ratio = split_ratio
        self.train = train

        if mask_dir is not None:
            self.mask_dir = Path(mask_dir)

        assert len(image_affix) == len(mask_affix) == 2, "Affix must be a tuple of length 2."
        self.image_affix = image_affix
        self.mask_affix = mask_affix

        self.transform: Callable | None = transform
        # Initialize the loader, very import to ensure channel first!
        self.loader = LoadImaged(keys=["image", "mask"], ensure_channel_first=True)

    @abstractmethod
    def get_image_mask_files(self) -> tuple[list[Path], list[Path]]:
        pass

    def __len__(self) -> int:
        image_files, _ = self.get_image_mask_files()
        return len(image_files)

    def init_cache(self):
        image_files, _ = self.get_image_mask_files()
        if self.cache_pooling != 0:
            result = np.array_split(range(len(image_files)), self.cache_pooling)
            with Pool(processes=self.cache_pooling) as pool:
                pooled_samples = pool.map(self.process_samples, [list(part) for part in result])
            unpacked_samples = list(zip(*pooled_samples, strict=False))
            self.image_cache = functools.reduce(operator.iadd, unpacked_samples[0], [])
            self.mask_cache = functools.reduce(operator.iadd, unpacked_samples[1], [])

        else:
            images, masks = self.process_samples(list(range(len(image_files))))
            self.image_cache = images
            self.mask_cache = masks

    def process_samples(
        self, indices: int | list[int]
    ) -> tuple[MetaTensor, MetaTensor] | tuple[list[MetaTensor], list[MetaTensor]]:
        """Apply the transformation to the image and mask files."""
        return_as_list = True
        if not isinstance(indices, list):
            return_as_list = False
            indices = [indices]

        image_files, mask_files = self.get_image_mask_files()
        images = []
        masks = []
        for idx in indices:
            data_dict = {
                "image": image_files[idx],
                "mask": mask_files[idx],
            }
            # Load images and metadata
            loaded_data = self.loader(data_dict)

            # Apply additional transformations if provided
            if self.transform:
                loaded_data = self.transform(loaded_data)

            # Extract the transformed image and mask and append to the output lists
            images.append(loaded_data["image"])
            masks.append(perform_mask_transformation(loaded_data["mask"], self.mask_operation))

        return (
            (
                images,
                masks,
            )
            if return_as_list
            else (
                images[0],
                masks[0],
            )
        )
        # Alternatively use len(output_list) > 1, but could result in unexpected behavior

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        return (
            (
                self.image_cache[idx],
                self.mask_cache[idx],
            )
            if self.use_cache
            else self.process_samples(idx)
        )

    @staticmethod
    def load_image_mask_files(
        image_regex: str, mask_regex: str, image_dir: Path, mask_dir: Path
    ) -> tuple[list[Path], list[Path]]:
        return (sorted(image_dir.glob(image_regex)), sorted(mask_dir.glob(mask_regex)))

    @staticmethod
    def check_img_mask_files(
        image_files,
        mask_files,
        data_dir,
        mask_dir,
        image_affix,
        mask_affix,
    ):
        if len(image_files) == 0:
            msg = f"No image files found. {data_dir}"
            raise ValueError(msg)

        if len(mask_files) == 0:
            msg = f"No mask files found. (mask_dir: {mask_dir})"
            raise ValueError(msg)

        # Ensure image and mask files match
        assert len(image_files) == len(
            mask_files
        ), "Number of image files and mask files must match."

        image_prefix, image_suffix = image_affix
        mask_prefix, mask_suffix = mask_affix
        for img, mask in zip(image_files, mask_files, strict=False):
            stripped_img = img.name.replace(image_suffix, "").replace(image_prefix, "")
            stripped_mask = mask.name.replace(mask_suffix, "").replace(mask_prefix, "")
            assert stripped_img == stripped_mask, f"Image and mask files must match. {img} {mask}"


class NiftiDataset(ABCNiftiDataset):
    """PyTorch Dataset for loading 3D NIfTI images and masks from a directory.

    Parameters:
        data_dir: Path to the directory containing image and mask files.
        image_suffix: Suffix or pattern to identify image files (default: '.img.nii.gz').
        mask_suffix: Suffix or pattern to identify mask files (default: '.label.nii.gz').
        transform: A function/transform to apply to both images and masks.
    """

    def __init__(
        self,
        data_dir: str | Path,
        mask_dir: str | Path | None = None,
        image_affix: tuple[str] = ("", ".img.nii.gz"),
        mask_affix: tuple[str] = ("", ".label.nii.gz"),
        train: bool = True,
        split_ratio: float = 0.9,
        max_samples: int | None = None,
        cache: bool = False,
        cache_pooling: int = 0,
        mask_operation: MaskOperations = MaskOperations.STD,
        **kwargs,
    ) -> None:
        super().__init__(
            data_dir=data_dir,
            mask_dir=mask_dir,
            image_affix=image_affix,
            mask_affix=mask_affix,
            train=train,
            split_ratio=split_ratio,
            max_samples=max_samples,
            cache=cache,
            cache_pooling=cache_pooling,
            mask_operation=mask_operation,
            **kwargs,
        )

        # Collect image and mask file paths
        image_files, mask_files = self.load_image_mask_files(
            f"{image_affix[0]}*{image_affix[1]}",
            f"{mask_affix[0]}*{mask_affix[1]}",
            self.data_dir,
            self.mask_dir,
        )

        # validate the files and ensure they match
        self.check_img_mask_files(
            image_files,
            mask_files,
            self.data_dir,
            self.mask_dir,
            image_affix,
            mask_affix,
        )

        # Split the dataset into training and validation sets
        self.image_files, self.mask_files = self.get_sample(
            image_files, mask_files, train=train, split_ratio=split_ratio
        )

        # limit files to max_samples
        if max_samples is not None:
            self.image_files = self.image_files[:max_samples]
            self.mask_files = self.mask_files[:max_samples]

        # init cache
        if cache:
            self.init_cache()

    @staticmethod
    def get_sample(
        image_files: list[Path],
        mask_files: list[Path],
        train: bool = True,
        split_ratio: float = 0.9,
    ) -> tuple[list[Path], list[Path]]:
        random.seed(42)
        data_files = list(zip(image_files, mask_files, strict=True))
        num_train = int(len(data_files) * split_ratio)
        all_indices = list(range(len(data_files)))
        train_indices = random.sample(all_indices, num_train)
        val_indices = list(set(all_indices) - set(train_indices))
        filtered_data_files = [data_files[i] for i in (train_indices if train else val_indices)]
        return zip(*filtered_data_files, strict=True)

    # @override
    def get_image_mask_files(self):
        return self.image_files, self.mask_files


class GroupedNifitDataset(ABCNiftiDataset):
    """PyTorch Dataset for loading preprocessed 3D NIfTI images and masks from a directory.

    Parameters:
        data_dir: Path to the directory containing image and mask files.
        image_suffix: Suffix or pattern to identify image files (default: '.img.nii.gz').
        mask_suffix: Suffix or pattern to identify mask files (default: '.label.nii.gz').
        transform: A function/transform to apply to both images and masks.
    """

    def __init__(
        self,
        data_dir: str | Path,
        mask_dir: str | Path | None = None,
        image_affix: tuple[str] = ("", ".img.nii.gz"),
        mask_affix: tuple[str] = ("", ".label.nii.gz"),
        max_samples: int | None = None,
        cache: bool = False,
        cache_pooling: int = 0,
        max_epoch: int = 1,
    ) -> None:
        super().__init__(
            data_dir=data_dir,
            mask_dir=mask_dir,
            image_affix=image_affix,
            mask_affix=mask_affix,
            train=True,
            max_samples=max_samples,
            cache=cache,
            cache_pooling=cache_pooling,
            mask_operation=MaskOperations.STD,
        )

        self.max_epoch = max_epoch
        self.epoch_counter = 0

        self.image_files, self.mask_files = [], []
        for epoch_num in range(self.max_epoch):
            # Collect image and mask file paths
            image_files, mask_files = self.load_image_mask_files(
                f"{self.image_affix[0]}*_patch[[]{epoch_num}[]]{self.image_affix[1]}",
                f"{self.mask_affix[0]}*_patch[[]{epoch_num}[]]{self.mask_affix[1]}",
                self.data_dir,
                self.mask_dir,
            )

            if max_samples is not None:
                image_files = image_files[:max_samples]
                mask_files = mask_files[:max_samples]

            # validate the files and ensure they match
            self.check_img_mask_files(
                image_files,
                mask_files,
                self.data_dir,
                self.mask_dir,
                self.image_affix,
                self.mask_affix,
            )

            self.image_files.append(image_files)
            self.mask_files.append(mask_files)

        assert (
            len({len(images) for images in self.image_files}) == 1
        ), "Number of images must be equal for all epochs."

        if cache:
            self.init_cache()

    # @override
    def get_image_mask_files(self):
        return (
            self.image_files[self.epoch_counter % self.max_epoch],
            self.mask_files[self.epoch_counter % self.max_epoch],
        )

    def next_epoch(self):
        self.epoch_counter += 1

        if self.use_cache:
            self.init_cache()


class GraphDataset(Dataset):
    def __init__(
        self, data_dir: str | Path = "/data/training_data", transform: Callable | None = None
    ):
        self.data_dir = Path(data_dir)
        self.relevant_ids = sorted(
            self.get_ids("img")
            .intersection(self.get_ids("label"))
            .intersection(self.get_ids("graph"))
        )

        print(f"Found {len(self.relevant_ids)} relevant IDs")
        self.image_files = [next(self.data_dir.glob(f"{id_}*img*")) for id_ in self.relevant_ids]
        self.mask_files = [next(self.data_dir.glob(f"{id_}*label*")) for id_ in self.relevant_ids]
        self.graph_files = [next(self.data_dir.glob(f"{id_}*graph*")) for id_ in self.relevant_ids]

        self.loader = LoadImaged(keys=["image", "mask"], ensure_channel_first=True)

        self.transform = transform or get_std_transform()

    def get_ids(self, item: str):
        return {p.stem.split(".")[0] for p in self.data_dir.glob(f"*{item}*")}

    def __len__(self):
        return len(self.relevant_ids)

    def __getitem__(self, idx):
        data_dict = {
            "image": self.image_files[idx],
            "mask": self.mask_files[idx],
        }
        # Load images and metadata
        loaded_data = self.loader(data_dict)
        transformed_data = self.transform(loaded_data)
        graph_file = self.graph_files[idx]

        return {
            "image": transformed_data["image"],
            "mask": transformed_data["mask"],
            "raw_image": loaded_data["image"],
            "raw_mask": loaded_data["mask"],
            "graph": graph_file,
        }


class ImageDataset(Dataset):
    def __init__(self, data_dir: str | Path, transform: Callable):
        self.image_files = sorted(Path(data_dir).glob("*img*"), key=lambda p: p.stem.split(".")[0])
        self.loader = LoadImage(ensure_channel_first=True)
        self.transform = transform

    def __len__(self):
        return len(self.image_files)

    def __getitem__(self, idx):
        return self.transform(self.loader(self.image_files[idx]))


class UnlabeledDataset(Dataset):
    def __init__(self, data_dir: str | Path = "/data/training_data", string_label: str = "label"):
        self.data_dir = Path(data_dir)
        self.files = sorted(
            self.data_dir.glob(f"*{string_label}*"), key=lambda p: p.stem.split(".")[0]
        )

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        return nib.load(self.files[idx])


def get_dataset(
    cfg: DataLoaderConfig,
) -> tuple[NiftiDataset, NiftiDataset] | tuple[GroupedNifitDataset, NiftiDataset]:
    """Return the training and validation datasets."""

    def _get_dataset(cfg: DatasetConfig):
        return (
            GroupedNifitDataset(
                data_dir=cfg.data_dir,
                mask_dir=cfg.mask_dir,
                image_affix=cfg.image_affix,
                mask_affix=cfg.mask_affix,
                max_samples=cfg.max_samples,
                cache=cfg.cache,
                cache_pooling=cfg.cache_pooling,
                max_epoch=cfg.max_epochs,
            )
            if cfg.grouped
            else NiftiDataset(
                data_dir=cfg.data_dir,
                mask_dir=cfg.mask_dir,
                image_affix=cfg.image_affix,
                mask_affix=cfg.mask_affix,
                transform=get_transform(
                    type_=cfg.transform,
                    size=cfg.size,
                    target_pixel_dim=cfg.target_pixel_dim,
                    target_spatial_size=cfg.target_spatial_size,
                    sigma_ratio=cfg.sigma_ratio,
                    pos_center_prob=cfg.pos_center_prob,
                ),
                train=cfg.train,
                split_ratio=cfg.split_ratio,
                max_samples=cfg.max_samples,
                cache=cfg.cache,
                cache_pooling=cfg.cache_pooling,
                mask_operation=cfg.mask_operation,
            )
        )

    cfg.train.train = True
    cfg.val.train = False
    return _get_dataset(cfg.train), _get_dataset(cfg.val)


##### Transformations #####


class TruncatedGaussianRandomCrop(Randomizable, MapTransform):
    def __init__(
        self,
        keys: KeysCollection,
        roi_size: Sequence[int],
        sigma_ratio: float = 0.1,
        allow_missing_keys: bool = False,
    ):
        """Transform to crop a random region from a volume based on a truncated Gaussian distribution.

        Args:
            keys: Keys of the corresponding items to be transformed.
            roi_size: Desired output size of the crop (e.g., Depth, Height, Width for 3D).
                      Can handle 2D, 3D, or higher-dimensional data.
            sigma_ratio: Ratio to determine the standard deviation of the Gaussian
                         distribution relative to volume dimensions.
            allow_missing_keys: Don't raise exception if key is missing.
        """
        super().__init__(keys, allow_missing_keys)
        self.roi_size = np.array(roi_size if isinstance(roi_size, Sequence) else [roi_size])
        self.sigma_ratio = sigma_ratio

    def truncated_normal(self, mean, std, lower, upper):
        """Generate a sample from a truncated normal distribution."""
        a, b = (lower - mean) / std, (upper - mean) / std
        return truncnorm.rvs(a, b, loc=mean, scale=std, random_state=self.R)

    def sample_center(self, img_shape):
        """Sample the center of the crop based on the truncated Gaussian distribution."""
        img_shape = np.array(img_shape)
        center = img_shape // 2
        sigma = img_shape * self.sigma_ratio

        crop_center = []
        for i in range(len(img_shape)):
            lower_bound = self.roi_size[i] // 2
            upper_bound = img_shape[i] - self.roi_size[i] // 2
            crop_center.append(
                int(self.truncated_normal(center[i], sigma[i], lower_bound, upper_bound))
            )

        return np.array(crop_center)

    def plot_distributions(self, img_shape, num_samples=10000):
        """Plot the distributions of the sampled crop center for each dimension.

        Note:
        - For 2D data, generates a heatmap for the sampled (x, y) coordinates.
        - For higher-dimensional data, generates histograms for each dimension.
        """
        img_shape = np.array(img_shape)
        centers = np.array([self.sample_center(img_shape) for _ in range(num_samples)])

        if len(img_shape) == 2:  # Special case: 2D data
            # Generate 2D histogram for x, y distribution
            heatmap, xedges, yedges = np.histogram2d(
                centers[:, 0],
                centers[:, 1],
                bins=(50, 50),
                range=[[0, img_shape[0]], [0, img_shape[1]]],
            )

            # Plot heatmap
            plt.figure(figsize=(8, 6))
            plt.imshow(
                heatmap.T,
                origin="lower",
                aspect="auto",
                extent=[xedges[0], xedges[-1], yedges[0], yedges[-1]],
                cmap="viridis",
            )
            plt.colorbar(label="Frequency")
            plt.title("2D Heatmap of Sampled Crop Centers (x, y)")
            plt.xlabel("Height (y)")
            plt.ylabel("Width (x)")
            plt.grid(False)
            plt.show()

        else:  # Default case: N-Dimensional data
            fig, axes = plt.subplots(1, len(img_shape), figsize=(5 * len(img_shape), 5))
            if len(img_shape) == 1:
                axes = [axes]  # Ensure axes is iterable for 1D case
            axis_labels = [f"Dim {i}" for i in range(len(img_shape))]

            for i, ax in enumerate(axes):
                ax.hist(centers[:, i], bins=50, alpha=0.7, color="blue", edgecolor="black")
                ax.set_title(f"Distribution of {axis_labels[i]} Sampled Crop Centers")
                ax.set_xlabel(f"{axis_labels[i]} Coordinate")
                ax.set_ylabel("Frequency")
                ax.grid(True)

            plt.tight_layout()
            plt.show()

    def __call__(self, data: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        d = dict(data)
        if not self.allow_missing_keys and not all(k in d for k in self.keys):
            msg = f"Not all keys {self.keys} not found in data dictionary"
            raise KeyError(msg)

        for key in self.keys:
            img = d[key]
            img_shape = np.array(img.shape[1:])  # Assuming channel-first format
            crop_center = self.sample_center(img_shape)

            # Calculate the start and end points for cropping
            start = crop_center - self.roi_size // 2
            end = start + self.roi_size

            # Extract the crop
            slices = tuple(slice(s, e) for s, e in zip(start, end, strict=False))
            d[key] = img[(slice(None), *slices)]

        return d


class PositiveBiasedRandomCrop(Randomizable, MapTransform):
    def __init__(
        self,
        keys,
        roi_size,
        positive_key,
        positive_probability=POS_CENTER_PROB,
        allow_missing_keys=False,
    ):
        """Randomly crops with a specified probability of centering the crop on a positive voxel.

        Args:
            keys: Keys of the corresponding items to be transformed.
            roi_size: Desired output size of the crop (e.g., Depth, Height, Width for 3D).
                      Can handle 2D, 3D, or higher-dimensional data.
            positive_key: Key for the segmentation mask used to determine positive voxels.
            positive_probability: Probability that the crop will center on a positive voxel.
            allow_missing_keys: Don't raise exception if key is missing.
        """
        super().__init__(keys, allow_missing_keys)
        self.roi_size = np.array(roi_size if isinstance(roi_size, list | tuple) else [roi_size])
        self.positive_key = positive_key
        self.positive_probability = positive_probability

    def sample_center_positive(self, mask):
        """Sample a crop center from positive voxels."""
        img_shape = np.array(mask.shape[1:])
        positive_mask = mask.squeeze() > 0  # Shape: (600, 600, 280)

        # Create boundary masks for each axis
        z_valid = np.arange(mask.shape[1]) >= self.roi_size[0] // 2
        z_valid &= np.arange(mask.shape[1]) < img_shape[0] - self.roi_size[0] // 2

        y_valid = np.arange(mask.shape[2]) >= self.roi_size[1] // 2
        y_valid &= np.arange(mask.shape[2]) < img_shape[1] - self.roi_size[1] // 2

        x_valid = np.arange(mask.shape[3]) >= self.roi_size[2] // 2
        x_valid &= np.arange(mask.shape[3]) < img_shape[2] - self.roi_size[2] // 2

        # Combine the positive mask with valid bounds for each axis
        valid_mask = (
            positive_mask & z_valid[:, None, None] & y_valid[None, :, None] & x_valid[None, None, :]
        )

        # Get positive voxel coordinates
        positive_voxels = np.argwhere(valid_mask)

        if len(positive_voxels) == 0:
            # No positive voxels found, sample randomly
            return self.sample_center_random(img_shape)

        return random.choice(positive_voxels)

    def sample_center_random(self, img_shape):
        """Sample a random crop center."""
        crop_center = [
            self.R.randint(self.roi_size[i] // 2, img_shape[i] - self.roi_size[i] // 2)
            for i in range(len(img_shape))
        ]
        return np.array(crop_center)

    def __call__(self, data):
        """Apply the crop transform to the input data."""
        d = dict(data)

        if not self.allow_missing_keys and not all(k in d for k in self.keys):
            msg = f"Keys {self.keys} not found in data dictionary"
            raise KeyError(msg)

        mask = d[self.positive_key]
        img_shape = np.array(mask.shape[1:])

        crop_center = (
            self.sample_center_positive(mask)
            if self.R.random() < self.positive_probability
            else self.sample_center_random(img_shape)
        )

        for key in self.keys:
            img = d[key]

            # Calculate start and end indices for cropping
            start = crop_center - self.roi_size // 2
            end = start + self.roi_size

            # Extract the crop
            slices = tuple(slice(s, e) for s, e in zip(start, end, strict=False))
            d[key] = img[(slice(None), *slices)]

        return d


def get_default_transforms(
    target_pixel_dim: tuple[float, float, float],
    target_spatial_size: tuple[int, int, int],
) -> list[MapTransform]:
    return [
        # 1) Resample the image and mask to have the same voxel spacing
        Spacingd(
            keys=["image", "mask"],
            pixdim=target_pixel_dim,
            mode=("bilinear", "nearest"),
        ),
        # 2) Scale the intensity of the image to [0, 1]
        # this really depends on the input range. it could happen that the range is not meaningful
        # NormalizeIntensityd(
        #    keys=["image"], subtrahend=DATASET_VALUE_MEAN, divisor=DATASET_VALUE_STD
        # ),
        ScaleIntensityd(keys=["image"], minv=0.0, maxv=1.0),
        # 3) Resize the image and mask to a target spatial size without distorting the aspect ratio
        ResizeWithPadOrCropd(
            keys=["image", "mask"],
            spatial_size=target_spatial_size,
            mode="edge",
        ),
        ToTensord(keys=["image", "mask"]),
    ]


def get_scaling_transforms(
    target_pixel_dim: tuple[float, float, float],
    target_spatial_size: tuple[int, int, int],
) -> list[MapTransform]:
    return [
        # 1) Resample the image and mask to have the same voxel spacing
        Spacingd(
            keys=["image", "mask"],
            pixdim=target_pixel_dim,
            mode=("bilinear", "nearest"),
        ),
        # 2) Scale the intensity of the image to [0, 1]
        # this really depends on the input range. it could happen that the range is not meaningful
        ScaleIntensityd(keys=["image"], minv=0.0, maxv=1.0),
        # 3) Resize the image and mask to a target spatial size without distorting the aspect ratio
        ResizeWithPadOrCropd(
            keys=["image", "mask"],
            spatial_size=target_spatial_size,
            mode="edge",
        ),
        ToTensord(keys=["image", "mask"]),
    ]


def get_resize_transform(
    size: Sequence[int] = TARGET_SPATIAL_SIZE,
    target_pixel_dim: tuple[float, float, float] = TARGET_PIXEL_DIM,
    target_spatial_size: tuple[int, int, int] = TARGET_SPATIAL_SIZE,
):
    # Make a copy of the pipeline transforms
    transforms = get_default_transforms(target_pixel_dim, target_spatial_size)
    return Compose(
        transforms[:-1]
        + [
            Resized(
                keys=["image", "mask"],
                spatial_size=size,
                mode=("bilinear", "nearest"),
            )
        ]
        + transforms[-1:]
    )


def get_patch_center_gaussian_transform(
    size: Sequence[int] = (96, 96, 96),
    target_pixel_dim: tuple[float, float, float] = TARGET_PIXEL_DIM,
    target_spatial_size: tuple[int, int, int] = TARGET_SPATIAL_SIZE,
    sigma_ratio: float = GOOD_SIGMA_RATIO,
):
    # Make a copy of the pipeline transforms
    transforms = get_default_transforms(target_pixel_dim, target_spatial_size)
    return Compose(
        transforms[:-1]
        + [
            TruncatedGaussianRandomCrop(
                keys=["image", "mask"],
                roi_size=size,
                sigma_ratio=sigma_ratio,
            )
        ]
        + transforms[-1:]
    )


def get_patch_positive_center_transform(
    size: Sequence[int] = (96, 96, 96),
    target_pixel_dim: tuple[float, float, float] = TARGET_PIXEL_DIM,
    target_spatial_size: tuple[int, int, int] = TARGET_SPATIAL_SIZE,
    pos_center_prob: float = POS_CENTER_PROB,
):
    # Make a copy of the pipeline transforms
    transforms = get_default_transforms(target_pixel_dim, target_spatial_size)
    return Compose(
        transforms[:-1]
        + [
            PositiveBiasedRandomCrop(
                keys=["image", "mask"],
                positive_key="mask",
                roi_size=size,
                positive_probability=pos_center_prob,
            )
        ]
        + transforms[-1:]
    )


def get_patch_uniform_transform(
    size: Sequence[int] = (96, 96, 96),
    target_pixel_dim: tuple[float, float, float] = TARGET_PIXEL_DIM,
    target_spatial_size: tuple[int, int, int] = TARGET_SPATIAL_SIZE,
):
    # Make a copy of the pipeline transforms
    transforms = get_default_transforms(target_pixel_dim, target_spatial_size)
    return Compose(
        transforms[:-1]
        + [
            RandSpatialCropd(
                keys=["image", "mask"],
                roi_size=size,
                random_size=False,
                random_center=True,
            )
        ]
        + transforms[-1:]
    )


def get_std_transform(
    target_pixel_dim: tuple[float, float, float] = TARGET_PIXEL_DIM,
    target_spatial_size: tuple[int, int, int] = TARGET_SPATIAL_SIZE,
):
    transforms = get_default_transforms(target_pixel_dim, target_spatial_size)
    return Compose(transforms)


def get_scaling_transform(
    target_pixel_dim: tuple[float, float, float] = TARGET_PIXEL_DIM,
    target_spatial_size: tuple[int, int, int] = TARGET_SPATIAL_SIZE,
):
    transforms = get_scaling_transforms(target_pixel_dim, target_spatial_size)
    return Compose(transforms)


def get_transform(
    type_: TransformType,
    size: Sequence[int] | int = 96,
    target_pixel_dim: tuple[float, float, float] = TARGET_PIXEL_DIM,
    target_spatial_size: tuple[int, int, int] = TARGET_SPATIAL_SIZE,
    sigma_ratio: float = GOOD_SIGMA_RATIO,
    pos_center_prob: float = POS_CENTER_PROB,
) -> Callable:
    """Get the transformation function based on the type."""
    size = [size] * 3 if isinstance(size, int) else size
    match type_:
        case TransformType.RESIZE:
            return get_resize_transform(size, target_pixel_dim, target_spatial_size)
        case TransformType.PATCH_CENTER_GAUSSIAN:
            return get_patch_center_gaussian_transform(
                size,
                target_pixel_dim,
                target_spatial_size,
                sigma_ratio,
            )
        case TransformType.PATCH_POS_CENTER:
            return get_patch_positive_center_transform(
                size,
                target_pixel_dim,
                target_spatial_size,
                pos_center_prob,
            )
        case TransformType.PATCH_UNIFORM:
            return get_patch_uniform_transform(
                size,
                target_pixel_dim,
                target_spatial_size,
            )
        case TransformType.STD:
            return get_std_transform(target_pixel_dim, target_spatial_size)
        case TransformType.TOTENSOR:
            return ToTensord(keys=["image", "mask"])
        case _:
            msg = f"Invalid transform type: {type_}"
            raise ValueError(msg)


def perform_mask_transformation(mask, mask_operation: MaskOperations):
    if mask_operation == MaskOperations.BINARY_CLASS:
        mask[mask != 1] = 0
        return mask
    return mask


def reshape_to_original(mask):
    original_shape = mask.meta.get("spatial_shape")
    original_pixel_spacing = mask.meta.get("pixdim")
    if original_shape is None:
        msg = "Original shape information is not available in the metadata."
        raise ValueError(msg)
    if original_pixel_spacing is None:
        msg = "Original pixel spacing information is not available in the metadata."
        raise ValueError(msg)
    return Compose(
        [
            Spacing(
                pixdim=original_pixel_spacing[1:4].tolist(),
                mode="bilinear",
            ),
            ResizeWithPadOrCrop(
                spatial_size=original_shape.tolist(),
                mode="constant",
            ),
        ]
    )(mask)
