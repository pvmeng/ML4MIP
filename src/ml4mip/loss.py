from dataclasses import dataclass
from enum import Enum

import torch
import torch.nn.functional as F
from monai.losses import DiceCELoss, DiceLoss, FocalLoss, TverskyLoss
from torch import nn


class LossType(Enum):
    DICE = "dice"
    CE_DICE = "ce_dice"
    FOCAL = "focal"
    TVERSKY = "tversky"
    SOFT_CL_DICE = "soft_cl_dice"
    SOFT_DICE_CL_DICE = "soft_dice_cl_dice"


@dataclass
class LossConfig:
    loss_type: LossType = LossType.CE_DICE
    lambda_dice: float = 1.0
    lambda_ce: float = 0.3
    cedice_batch: bool = False
    alpha: float = 0.5
    sigmoid = True


class SoftSkeletonize(torch.nn.Module):
    def __init__(self, num_iter=40):
        super(SoftSkeletonize, self).__init__()
        self.num_iter = num_iter

    def soft_erode(self, img):
        if len(img.shape) == 4:
            p1 = -F.max_pool2d(-img, (3, 1), (1, 1), (1, 0))
            p2 = -F.max_pool2d(-img, (1, 3), (1, 1), (0, 1))
            return torch.min(p1, p2)
        if len(img.shape) == 5:
            p1 = -F.max_pool3d(-img, (3, 1, 1), (1, 1, 1), (1, 0, 0))
            p2 = -F.max_pool3d(-img, (1, 3, 1), (1, 1, 1), (0, 1, 0))
            p3 = -F.max_pool3d(-img, (1, 1, 3), (1, 1, 1), (0, 0, 1))
            return torch.min(torch.min(p1, p2), p3)
        return None

    def soft_dilate(self, img):
        if len(img.shape) == 4:
            return F.max_pool2d(img, (3, 3), (1, 1), (1, 1))
        if len(img.shape) == 5:
            return F.max_pool3d(img, (3, 3, 3), (1, 1, 1), (1, 1, 1))
        return None

    def soft_open(self, img):
        return self.soft_dilate(self.soft_erode(img))

    def soft_skel(self, img):
        img1 = self.soft_open(img)
        skel = F.relu(img - img1)

        for j in range(self.num_iter):
            img = self.soft_erode(img)
            img1 = self.soft_open(img)
            delta = F.relu(img - img1)
            skel = skel + F.relu(delta - skel * delta)

        return skel

    def forward(self, img):
        return self.soft_skel(img)


class soft_cldice(nn.Module):
    def __init__(self, iter_=3, smooth=1.0, exclude_background=False, sigmoid=True):
        super(soft_cldice, self).__init__()
        self.iter = iter_
        self.smooth = smooth
        self.soft_skeletonize = SoftSkeletonize(num_iter=10)
        self.exclude_background = exclude_background
        self.sigmoid = sigmoid

    def forward(self, y_true, y_pred):
        if self.exclude_background:
            y_true = y_true[:, 1:, :, :]
            y_pred = y_pred[:, 1:, :, :]

        if self.sigmoid:
            y_pred = torch.sigmoid(y_pred)

        skel_pred = self.soft_skeletonize(y_pred)
        skel_true = self.soft_skeletonize(y_true)
        tprec = (torch.sum(torch.multiply(skel_pred, y_true)) + self.smooth) / (
            torch.sum(skel_pred) + self.smooth
        )
        tsens = (torch.sum(torch.multiply(skel_true, y_pred)) + self.smooth) / (
            torch.sum(skel_true) + self.smooth
        )
        cl_dice = 1.0 - 2.0 * (tprec * tsens) / (tprec + tsens)
        return cl_dice


def soft_dice(y_true, y_pred):
    """[function to compute dice loss]

    Args:
        y_true ([float32]): [ground truth image]
        y_pred ([float32]): [predicted image]

    Returns:
        [float32]: [loss value]
    """
    smooth = 1
    intersection = torch.sum(y_true * y_pred)
    coeff = (2.0 * intersection + smooth) / (torch.sum(y_true) + torch.sum(y_pred) + smooth)
    return 1.0 - coeff


class soft_dice_cldice(nn.Module):
    def __init__(self, iter_=3, alpha=0.5, smooth=1.0, exclude_background=False):
        super(soft_dice_cldice, self).__init__()
        self.iter = iter_
        self.smooth = smooth
        self.alpha = alpha
        self.soft_skeletonize = SoftSkeletonize(num_iter=10)
        self.exclude_background = exclude_background

    def forward(self, y_true, y_pred):
        if self.exclude_background:
            y_true = y_true[:, 1:, :, :]
            y_pred = y_pred[:, 1:, :, :]
        dice = soft_dice(y_true, y_pred)
        skel_pred = self.soft_skeletonize(y_pred)
        skel_true = self.soft_skeletonize(y_true)
        tprec = (torch.sum(torch.multiply(skel_pred, y_true)) + self.smooth) / (
            torch.sum(skel_pred) + self.smooth
        )
        tsens = (torch.sum(torch.multiply(skel_true, y_pred)) + self.smooth) / (
            torch.sum(skel_true) + self.smooth
        )
        cl_dice = 1.0 - 2.0 * (tprec * tsens) / (tprec + tsens)
        return (1.0 - self.alpha) * dice + self.alpha * cl_dice


def get_loss(cfg: LossConfig):
    match cfg.loss_type:
        case LossType.DICE:
            return DiceLoss(include_background=True, sigmoid=cfg.sigmoid)
        case LossType.CE_DICE:
            return DiceCELoss(
                include_background=True,
                to_onehot_y=False,
                softmax=False,
                sigmoid=cfg.sigmoid,
                lambda_dice=cfg.lambda_dice,
                lambda_ce=cfg.lambda_ce,
                batch=cfg.cedice_batch,
            )
        case LossType.FOCAL:
            return FocalLoss(sigmoid=cfg.sigmoid)
        case LossType.TVERSKY:
            return TverskyLoss(sigmoid=cfg.sigmoid)
        case LossType.SOFT_CL_DICE:
            return soft_cldice()
        case LossType.SOFT_DICE_CL_DICE:
            return soft_dice_cldice(alpha=cfg.alpha)
        case _:
            msg = f"Loss type {cfg.loss_type} not supported"
            raise ValueError(msg)
