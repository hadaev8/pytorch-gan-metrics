from typing import List, Union, Tuple

import numpy as np
import torch
from scipy import linalg
from tqdm import tqdm
from torch.utils.data import DataLoader

from .inception import InceptionV3


# device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

@torch.no_grad()
def get_inception_feature(
    dataloader: DataLoader,
    dims: List[int],
    use_torch: bool = False,
    verbose: bool = False,
    device: torch.device = torch.device('cuda:0'),
) -> Union[torch.FloatTensor, np.ndarray]:
    """Calculate Inception Score and FID.
    For each image, only a forward propagation is required to
    calculating features for FID and Inception Score.

    Args:
        dataloader: torch.utils.data.Dataloader. The return image
            must be float tensor of range [0, 1].
        dims: List of int, see InceptionV3.BLOCK_INDEX_BY_DIM for
            available dimension.
        use_torch: bool. The default value is False and the backend is same as
            official implementation, i.e., numpy. If use_torch is enableb,
            the backend linalg is implemented by torch, the results are not
            guaranteed to be consistent with numpy, but the speed can be
            accelerated by GPU.
        verbose: Set verbose to False for disabling progress bar. Otherwise,
            the progress bar is showing when calculating activations.
        device: the torch device which is used to calculate inception feature
    Returns:
        inception_score: float tuple, (mean, std)
        fid: float
    """
    assert all(dim in InceptionV3.BLOCK_INDEX_BY_DIM for dim in dims)

    block_idxs = [InceptionV3.BLOCK_INDEX_BY_DIM[dim] for dim in dims]
    model = InceptionV3(block_idxs).to(device)
    model.eval()
    features = [[] for dim in dims]

    for batch_images in tqdm(dataloader, dynamic_ncols=True, leave=False,
        disable=not verbose, desc="get_inception_feature"):
        outputs = model(batch_images.to(device))
        for feature, output in zip(features, outputs):
            feature.append(output.squeeze(-1).squeeze(-1))
    features = [torch.cat(feature, dim=0) for feature in features]
    if not use_torch:
        features = [feature.cpu.numpy() for feature in features]
    return features


def torch_cov(m, rowvar=False):
    '''Estimate a covariance matrix given data.
    Covariance indicates the level to which two variables vary together.
    If we examine N-dimensional samples, `X = [x_1, x_2, ... x_N]^T`,
    then the covariance matrix element `C_{ij}` is the covariance of
    `x_i` and `x_j`. The element `C_{ii}` is the variance of `x_i`.
    Args:
        m: A 1-D or 2-D array containing multiple variables and observations.
            Each row of `m` represents a variable, and each column a single
            observation of all those variables.
        rowvar: If `rowvar` is True, then each row represents a
            variable, with observations in the columns. Otherwise, the
            relationship is transposed: each column represents a variable,
            while the rows contain observations.
    Returns:
        The covariance matrix of the variables.
    '''
    if m.dim() > 2:
        raise ValueError('m has more than 2 dimensions')
    if m.dim() < 2:
        m = m.view(1, -1)
    if not rowvar and m.size(0) != 1:
        m = m.t()
    return torch.cov(m)


# Pytorch implementation of matrix sqrt, from Tsung-Yu Lin, and Subhransu Maji
# https://github.com/msubhransu/matrix-sqrt
def sqrt_newton_schulz(A, numIters, dtype=None):
    with torch.no_grad():
        if dtype is None:
            dtype = A.type()
        batchSize = A.shape[0]
        dim = A.shape[1]
        normA = A.mul(A).sum(dim=1).sum(dim=1).sqrt()
        Y = A.div(normA.view(batchSize, 1, 1).expand_as(A))
        K = torch.eye(dim, dim).view(1, dim, dim).repeat(batchSize, 1, 1)
        Z = torch.eye(dim, dim).view(1, dim, dim).repeat(batchSize, 1, 1)
        K = K.type(dtype)
        Z = Z.type(dtype)
        for i in range(numIters):
            T = 0.5 * (3.0 * K - Z.bmm(Y))
            Y = Y.bmm(T)
            Z = T.bmm(Z)
        sA = Y*torch.sqrt(normA).view(batchSize, 1, 1).expand_as(A)
    return sA


def calculate_frechet_inception_distance(
    acts: Union[torch.FloatTensor, np.ndarray],
    mu: np.ndarray,
    sigma: np.ndarray,
    use_torch: bool = False,
    eps: float = 1e-6,
    device: torch.device = torch.device('cuda:0'),
) -> float:
    if use_torch:
        m1 = torch.mean(acts, axis=0)
        s1 = torch_cov(acts, rowvar=False)
        mu = torch.tensor(mu).to(m1.dtype).to(device)
        sigma = torch.tensor(sigma).to(s1.dtype).to(device)
    else:
        m1 = np.mean(acts, axis=0)
        s1 = np.cov(acts, rowvar=False)
    return calculate_frechet_distance(m1, s1, mu, sigma, use_torch, eps)


def calculate_frechet_distance(
    mu1: Union[torch.FloatTensor, np.ndarray],
    sigma1: Union[torch.FloatTensor, np.ndarray],
    mu2: Union[torch.FloatTensor, np.ndarray],
    sigma2: Union[torch.FloatTensor, np.ndarray],
    use_torch: bool = False,
    eps: float = 1e-6,
) -> float:
    """Frechet Distance
    Args:
        mu1: The sample mean over activations for generated samples.
        mu2: The sample mean over activations, precalculated on an reference
            data set.
        sigma1: The covariance matrix over activations for generated samples.
        sigma2: The covariance matrix over activations, precalculated on an
            reference data set.
        eps: prevent covmean from being singular matrix
        use_torch: use torch as backend

    Returns:
        The Frechet Distance.
    """

    if use_torch:
        assert mu1.shape == mu2.shape, \
            'Training and test mean vectors have different lengths'
        assert sigma1.shape == sigma2.shape, \
            'Training and test covariances have different dimensions'

        diff = mu1 - mu2
        # Run 50 itrs of newton-schulz to get the matrix sqrt of
        # sigma1 dot sigma2
        covmean = sqrt_newton_schulz(sigma1.mm(sigma2).unsqueeze(0), 50)
        if torch.any(torch.isnan(covmean)):
            return float('nan')
        covmean = covmean.squeeze()
        out = (diff.dot(diff) +
               torch.trace(sigma1) +
               torch.trace(sigma2) -
               2 * torch.trace(covmean)).cpu().item()
    else:
        mu1 = np.atleast_1d(mu1)
        mu2 = np.atleast_1d(mu2)

        sigma1 = np.atleast_2d(sigma1)
        sigma2 = np.atleast_2d(sigma2)

        assert mu1.shape == mu2.shape, \
            'Training and test mean vectors have different lengths'
        assert sigma1.shape == sigma2.shape, \
            'Training and test covariances have different dimensions'

        diff = mu1 - mu2

        # Product might be almost singular
        covmean, _ = linalg.sqrtm(sigma1.dot(sigma2), disp=False)
        if not np.isfinite(covmean).all():
            msg = ('fid calculation produces singular product; '
                   'adding %s to diagonal of cov estimates') % eps
            print(msg)
            offset = np.eye(sigma1.shape[0]) * eps
            covmean = linalg.sqrtm((sigma1 + offset).dot(sigma2 + offset))

        # Numerical error might give slight imaginary component
        if np.iscomplexobj(covmean):
            if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-3):
                m = np.max(np.abs(covmean.imag))
                raise ValueError('Imaginary component {}'.format(m))
            covmean = covmean.real

        tr_covmean = np.trace(covmean)

        out = (diff.dot(diff) +
               np.trace(sigma1) +
               np.trace(sigma2) -
               2 * tr_covmean)
    return out


def calculate_inception_score(
    probs: Union[torch.FloatTensor, np.ndarray],
    splits: int = 10,
    use_torch: bool = False,
) -> Tuple[float, float]:
    # Inception Score
    scores = []
    for i in range(splits):
        part = probs[
            (i * probs.shape[0] // splits):
            ((i + 1) * probs.shape[0] // splits), :]
        if use_torch:
            kl = part * (
                torch.log(part) -
                torch.log(torch.unsqueeze(torch.mean(part, 0), 0)))
            kl = torch.mean(torch.sum(kl, 1))
            scores.append(torch.exp(kl))
        else:
            kl = part * (
                np.log(part) -
                np.log(np.expand_dims(np.mean(part, 0), 0)))
            kl = np.mean(np.sum(kl, 1))
            scores.append(np.exp(kl))
    if use_torch:
        scores = torch.stack(scores)
        inception_score = torch.mean(scores).cpu().item()
        std = torch.std(scores).cpu().item()
    else:
        inception_score, std = (np.mean(scores), np.std(scores))
    del probs, scores
    return inception_score, std
