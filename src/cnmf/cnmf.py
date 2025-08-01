#!/usr/bin/env python

import numpy as np
import pandas as pd
import os, errno, sys
import datetime
import uuid
import itertools
import yaml
import subprocess
import scipy.sparse as sp
import warnings

from scipy.spatial.distance import squareform
# from sklearn.decomposition import non_negative_factorization
import torch
from nmf import run_nmf
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.metrics.pairwise import euclidean_distances
from sklearn.utils import sparsefuncs
from sklearn.preprocessing import StandardScaler

from scipy.cluster.hierarchy import leaves_list, linkage

import matplotlib.pyplot as plt

import scanpy as sc

from multiprocessing import Pool 

def save_df_to_npz(obj, filename):
    np.savez_compressed(filename, data=obj.values, index=obj.index.values, columns=obj.columns.values)

def save_df_to_text(obj, filename):
    obj.to_csv(filename, sep='\t')

def load_df_from_npz(filename):
    with np.load(filename, allow_pickle=True) as f:
        obj = pd.DataFrame(**f)
    return obj

def check_dir_exists(path):
    """
    Checks if directory already exists or not and creates it if it doesn't
    """
    try:
        os.makedirs(path)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise

def worker_filter(iterable, worker_index, total_workers):
    return (p for i,p in enumerate(iterable) if (i-worker_index)%total_workers==0)

def efficient_ols_all_cols(X, Y, batch_size=1024, normalize_y=False):
    """
    Solve OLS: Beta = (X^T X)^{-1} X^T Y,
    accumulating X^T X and X^T Y in row-batches.
    
    Optionally mean/variance-normalize each column of Y *globally* 
    (using the entire dataset's mean/var), while still only converting
    each row-batch to dense on-the-fly.

    Parameters
    ----------
    X : np.ndarray, shape (n_samples, n_predictors)
        Predictor matrix.
    Y : np.ndarray or scipy.sparse.spmatrix, shape (n_samples, n_targets)
        Outcomes. Each column is one target variable.
    batch_size : int
        Number of rows to process per chunk.
    normalize_y : bool
        If True, compute global mean & var of Y columns, then subtract mean 
        and divide by std for each batch.

    Returns
    -------
    Beta : np.ndarray, shape (n_predictors, n_targets)
        The OLS coefficients for each target.
    """

    # -- Basic shape checks
    n_samples, n_predictors = X.shape
    n_samples_Y, n_targets = Y.shape
    if n_samples != n_samples_Y:
        raise ValueError("X and Y must have the same number of rows.")

    # -- Optionally compute global mean & variance of Y columns
    if normalize_y:
        meanY, varY = get_mean_var(Y)

        # Avoid zero or near-zero std
        eps = 1e-12
        varY[varY < eps] = eps
        stdY = np.sqrt(varY)

    # -- Initialize accumulators
    XtX = np.zeros((n_predictors, n_predictors), dtype=np.float64)
    XtY = np.zeros((n_predictors, n_targets),    dtype=np.float64)

    # -- Process rows in batches
    for start_row in range(0, n_samples, batch_size):
        end_row = min(start_row + batch_size, n_samples)
        X_batch = X[start_row:end_row, :]

        # Extract chunk from Y.  If sparse, convert only this subset to dense.
        if sp.issparse(Y) and normalize_y:
            # Only need to densify if normalizing
            Y_batch = Y[start_row:end_row, :].toarray()
        else:
            Y_batch = Y[start_row:end_row, :]

        # -- Optionally apply normalization
        if normalize_y:
            Y_batch = (Y_batch - meanY) / stdY
        
        # -- Accumulate partial sums
        XtX += X_batch.T @ X_batch
        XtY += X_batch.T @ Y_batch

    # -- Solve the normal equations
    #    Beta = (X^T X)^(-1) X^T Y
    #    Using lstsq for stability.
    Beta, residuals, rank, s = np.linalg.lstsq(XtX, XtY, rcond=None)
    return Beta

def get_mean_var(X):
    scaler = StandardScaler(with_mean=False)
    scaler.fit(X)
    return(scaler.mean_, scaler.var_)

def get_highvar_genes_sparse(expression, expected_fano_threshold=None,
                       minimal_mean=0.5, numgenes=None):
    # Find high variance genes within those cells

    
    gene_mean, gene_var = get_mean_var(expression)
    gene_mean = pd.Series(gene_mean)
    gene_var = pd.Series(gene_var)
    gene_fano = gene_var / gene_mean

    # Find parameters for expected fano line
    top_genes = gene_mean.sort_values(ascending=False)[:20].index
    A = (np.sqrt(gene_var)/gene_mean)[top_genes].min()
    
    w_mean_low, w_mean_high = gene_mean.quantile([0.10, 0.90])
    w_fano_low, w_fano_high = gene_fano.quantile([0.10, 0.90])
    winsor_box = ((gene_fano > w_fano_low) &
                    (gene_fano < w_fano_high) &
                    (gene_mean > w_mean_low) &
                    (gene_mean < w_mean_high))
    fano_median = gene_fano[winsor_box].median()
    B = np.sqrt(fano_median)

    gene_expected_fano = (A**2)*gene_mean + (B**2)
    fano_ratio = (gene_fano/gene_expected_fano)

    # Identify high var genes
    if numgenes is not None:
        highvargenes = fano_ratio.sort_values(ascending=False).index[:numgenes]
        high_var_genes_ind = fano_ratio.index.isin(highvargenes)
        T=None

    else:
        if not expected_fano_threshold:
            T = (1. + gene_fano[winsor_box].std())
        else:
            T = expected_fano_threshold

        high_var_genes_ind = (fano_ratio > T) & (gene_mean > minimal_mean)

    gene_counts_stats = pd.DataFrame({
        'mean': gene_mean,
        'var': gene_var,
        'fano': gene_fano,
        'expected_fano': gene_expected_fano,
        'high_var': high_var_genes_ind,
        'fano_ratio': fano_ratio
        })
    gene_fano_parameters = {
            'A': A, 'B': B, 'T':T, 'minimal_mean': minimal_mean,
        }
    return gene_counts_stats, gene_fano_parameters



def get_highvar_genes(input_counts, expected_fano_threshold=None,
                       minimal_mean=0.5, numgenes=None):
    # Find high variance genes within those cells
    gene_counts_mean = pd.Series(input_counts.mean(axis=0).astype(float))
    gene_counts_var = pd.Series(input_counts.var(ddof=0, axis=0).astype(float))
    gene_counts_fano = pd.Series(gene_counts_var/gene_counts_mean)

    # Find parameters for expected fano line
    top_genes = gene_counts_mean.sort_values(ascending=False)[:20].index
    A = (np.sqrt(gene_counts_var)/gene_counts_mean)[top_genes].min()

    w_mean_low, w_mean_high = gene_counts_mean.quantile([0.10, 0.90])
    w_fano_low, w_fano_high = gene_counts_fano.quantile([0.10, 0.90])
    winsor_box = ((gene_counts_fano > w_fano_low) &
                    (gene_counts_fano < w_fano_high) &
                    (gene_counts_mean > w_mean_low) &
                    (gene_counts_mean < w_mean_high))
    fano_median = gene_counts_fano[winsor_box].median()
    B = np.sqrt(fano_median)

    gene_expected_fano = (A**2)*gene_counts_mean + (B**2)

    fano_ratio = (gene_counts_fano/gene_expected_fano)

    # Identify high var genes
    if numgenes is not None:
        highvargenes = fano_ratio.sort_values(ascending=False).index[:numgenes]
        high_var_genes_ind = fano_ratio.index.isin(highvargenes)
        T=None


    else:
        if not expected_fano_threshold:
            T = (1. + gene_counts_fano[winsor_box].std())
        else:
            T = expected_fano_threshold

        high_var_genes_ind = (fano_ratio > T) & (gene_counts_mean > minimal_mean)

    gene_counts_stats = pd.DataFrame({
        'mean': gene_counts_mean,
        'var': gene_counts_var,
        'fano': gene_counts_fano,
        'expected_fano': gene_expected_fano,
        'high_var': high_var_genes_ind,
        'fano_ratio': fano_ratio
        })
    gene_fano_parameters = {
            'A': A, 'B': B, 'T':T, 'minimal_mean': minimal_mean,
        }
    return gene_counts_stats, gene_fano_parameters


def compute_tpm(input_counts):
    """
    Default TPM normalization
    """
    tpm = input_counts.copy()
    sc.pp.normalize_total(tpm, target_sum=1e6)
    return tpm


# def factorize_mp_signature(args):
#     """
#     wrapper around factorize to be able to use mp pool.
#     args is a list:
#     worker-i: int
#     total_workers: int
#     pointer to nmf object.
#     """
#     args[2].factorize(worker_i=args[0],  total_workers=args[1])

def fit_H_online(
    X,
    W,
    H_init=None,
    chunk_size=5000,
    chunk_max_iter=200,
    h_tol=0.05,
    l1_reg_H=0.0,
    l2_reg_H=0.0,
    epsilon=1e-16,
    device="cpu"
    ):

    """
    Online MU to fit H only, given fixed W, accepts NumPy arrays or pandas DataFrames.

    Parameters
    ----------
    X : np.ndarray or pd.DataFrame, shape (n_samples, n_features)
        Non-negative data matrix.
    W : np.ndarray or pd.DataFrame, shape (n_components, n_features)
        Fixed basis matrix.
    H_init : np.ndarray or pd.DataFrame or None
        Initial guess for H; random non-negative if None.
    chunk_size : int
        Number of rows per online chunk.
    chunk_max_iter : int
        Max MU iterations per chunk.
    h_tol : float
        Local convergence tolerance for H updates.
    l1_reg_H : float
        L1 regularization strength on H.
    l2_reg_H : float
        L2 regularization strength on H.
    epsilon : float
        Small constant to avoid division by zero.
    device : str
        Torch device, e.g. "cpu" or "cuda".

    Returns
    -------
    H : np.ndarray or pd.DataFrame
        Fitted coefficient matrix H. If X and W are DataFrames, returns a DataFrame
        with the same index as X and component labels from W/index or "comp_i".
    """
    # Detect pandas inputs
    X_df = isinstance(X, pd.DataFrame)
    W_df = isinstance(W, pd.DataFrame)

    # Extract numpy arrays and remember labels
    X_index = None
    if X_df:
        X_index = X.index
        X = X.values
        
    W_index=None
    if W_df:
        comp_labels = list(W.index)
        W = W.values
    else:
        comp_labels = [f"comp_{i}" for i in range(W.shape[0])]

    # Handle H_init labels if provided
    if isinstance(H_init, pd.DataFrame):
        H_index = H_init.index
        H_init = H_init.values
    else:
        H_index = X_index if X_df else None

    if sp.issparse(X):
        X = X.toarray()

    # Move data to torch
    dtype = torch.float32
    dev = torch.device(device)
    X_t = torch.from_numpy(X).to(dtype=dtype, device=dev)
    W_t = torch.from_numpy(W).to(dtype=dtype, device=dev)

    n, _ = X_t.shape
    k, _ = W_t.shape

    # Initialize H
    if H_init is None:
        H_t = torch.rand((n, k), dtype=dtype, device=dev)
    else:
        H_t = torch.from_numpy(H_init).to(dtype=dtype, device=dev).clamp(min=0.0)

    # Precompute W Wᵀ
    WWT = W_t @ W_t.T

    # One pass through data in chunks
    idx = 0
    while idx < n:
        sl = slice(idx, idx + chunk_size)
        x = X_t[sl]
        h = H_t[sl]

        # Numerator for MU
        xWT = x @ W_t.T
        if l1_reg_H > 0:
            numer = (xWT - l1_reg_H).clamp(min=0.0)
        else:
            numer = xWT

        # MU inner loop
        for _ in range(chunk_max_iter):
            denom = h @ WWT
            if l2_reg_H > 0:
                denom = denom + l2_reg_H * h

            rates = numer / denom
            rates[denom < epsilon] = 0.0
            h_new = h * rates

            # Check local convergence
            rel = torch.norm(h_new - h) / (torch.norm(h) + epsilon)
            h = h_new
            if rel < h_tol:
                break

        H_t[sl] = h
        idx += chunk_size

    H_np = H_t.cpu().numpy()

    # Return as DataFrame if requested
    # if X_df or W_df:
    #     return pd.DataFrame(H_np, index=X_index, columns=comp_labels)
    return H_np

class cNMF():


    def __init__(self, output_dir=".", name=None):
        """
        Parameters
        ----------

        output_dir : path, optional (default=".")
            Output directory for analysis files.

        name : string, optional (default=None)
            A name for this analysis. Will be prefixed to all output files.
            If set to None, will be automatically generated from date (and random string).
        """

        self.output_dir = output_dir
        if name is None:
            now = datetime.datetime.now()
            rand_hash =  uuid.uuid4().hex[:6]
            name = '%s_%s' % (now.strftime("%Y_%m_%d"), rand_hash)
        self.name = name
        self.paths = None
        self._initialize_dirs()


    def _initialize_dirs(self):
        if self.paths is None:
            # Check that output directory exists, create it if needed.
            check_dir_exists(self.output_dir)
            check_dir_exists(os.path.join(self.output_dir, self.name))
            check_dir_exists(os.path.join(self.output_dir, self.name, 'cnmf_tmp'))

            self.paths = {
                'normalized_counts' : os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.norm_counts.h5ad'),
                'nmf_replicate_parameters' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.nmf_params.df.npz'),
                'nmf_run_parameters' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.nmf_idvrun_params.yaml'),
                'nmf_genes_list' :  os.path.join(self.output_dir, self.name, self.name+'.overdispersed_genes.txt'),

                'tpm' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.tpm.h5ad'),
                'tpm_stats' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.tpm_stats.df.npz'),

                'iter_spectra' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.spectra.k_%d.iter_%d.df.npz'),
                'iter_usages' :  os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.usages.k_%d.iter_%d.df.npz'),
                'merged_spectra': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.spectra.k_%d.merged.df.npz'),

                'local_density_cache': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.local_density_cache.k_%d.merged.df.npz'),
                'consensus_spectra': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.spectra.k_%d.dt_%s.consensus.df.npz'),
                'consensus_spectra__txt': os.path.join(self.output_dir, self.name, self.name+'.spectra.k_%d.dt_%s.consensus.txt'),
                'consensus_usages': os.path.join(self.output_dir, self.name, 'cnmf_tmp',self.name+'.usages.k_%d.dt_%s.consensus.df.npz'),
                'consensus_usages__txt': os.path.join(self.output_dir, self.name, self.name+'.usages.k_%d.dt_%s.consensus.txt'),

                'consensus_stats': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.stats.k_%d.dt_%s.df.npz'),

                'clustering_plot': os.path.join(self.output_dir, self.name, self.name+'.clustering.k_%d.dt_%s.png'),
                'gene_spectra_score': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.gene_spectra_score.k_%d.dt_%s.df.npz'),
                'gene_spectra_score__txt': os.path.join(self.output_dir, self.name, self.name+'.gene_spectra_score.k_%d.dt_%s.txt'),
                'gene_spectra_tpm': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.gene_spectra_tpm.k_%d.dt_%s.df.npz'),
                'gene_spectra_tpm__txt': os.path.join(self.output_dir, self.name, self.name+'.gene_spectra_tpm.k_%d.dt_%s.txt'),
                
                'starcat_spectra': os.path.join(self.output_dir, self.name, 'cnmf_tmp', self.name+'.starcat_spectra.k_%d.dt_%s.df.npz'),
                'starcat_spectra__txt': os.path.join(self.output_dir, self.name, self.name+'.starcat_spectra.k_%d.dt_%s.txt'),

                'k_selection_plot' :  os.path.join(self.output_dir, self.name, self.name+'.k_selection.png'),
                'k_selection_stats' :  os.path.join(self.output_dir, self.name, self.name+'.k_selection_stats.df.npz'),
            }


    def prepare(self, counts_fn, components, n_iter = 100, densify=False, tpm_fn=None, seed=None,
                        beta_loss='frobenius',num_highvar_genes=2000, genes_file=None,
                        alpha_usage=0.0, alpha_spectra=0.0, init='random', 
                        total_workers=-1, use_gpu=False, batch_size=5000, max_NMF_iter=1000):
        """
        Load input counts, reduce to high-variance genes, and variance normalize genes.
        Prepare file for distributing jobs over workers.


        Parameters
        ----------
        counts_fn : str
            Path to input counts matrix. If extension is .h5ad, .mtx, mtx.gz, or .npz, data is loaded
            accordingly. Otherwise it is assumed to be a tab-delimited text file. If .mtx or .mtx.gz, it
            is assumed to be in a 10x-Genomics-formatted mtx directory.

        components : list or numpy array
            Values of K to run NMF for
            
        n_iter : integer, optional (defailt=100)
            Number of iterations for factorization. If several ``k`` are specified, this many
            iterations will be run for each value of ``k``.

        densify : boolean, optional (default=False)
            Convert sparse data to dense

        tpm_fn : str or None, optional (default=None)
            If provided, load tpm data from file. Otherwise will compute it from the counts file
            
        seed : int or None, optional (default=None)
            Seed for sklearn random state.
            
        beta_loss : str or None, optional (default='frobenius')

        num_highvar_genes : int or None, optional (default=2000)
            If provided and genes_file is None, will compute this many highvar genes to use for factorization
        
        genes_file : str or None, optional (default=None)
            If provided will load high-variance genes from a list of these genes
            
        alpha_usage : float, optional (default=0.0)
            Regularization parameter for NMF corresponding to alpha_W in scikit-learn

        alpha_spectra : float, optional (default=0.0)
            Regularization parameter for NMF corresponding to alpha_H in scikit-learn
        
        total_workers : int, optional (default=-1)
            Number of cpu cores to use. By default all are used.

        use_gpuL bool, optional (default=False)
            Whether to use GPU.

        batch_size : int, optional (default=5000)
            Batch size for online NMF leaning.

        max_NMF_iter : int, optional (default=1000)
            Maximum number of iterations per individual NMF run
        """
        
        
        if counts_fn.endswith('.h5ad'):
            input_counts = sc.read(counts_fn)
        elif counts_fn.endswith('.mtx') or counts_fn.endswith('.mtx.gz'):
            counts_dir = os.path.dirname(counts_fn)
            input_counts = sc.read_10x_mtx(path = counts_dir)            
        else:
            ## Load txt or compressed dataframe and convert to scanpy object
            if counts_fn.endswith('.npz'):
                input_counts = load_df_from_npz(counts_fn)        
            else:
                input_counts = pd.read_csv(counts_fn, sep='\t', index_col=0)
                
            if densify:
                input_counts = sc.AnnData(X=input_counts.values,
                                       obs=pd.DataFrame(index=input_counts.index),
                                       var=pd.DataFrame(index=input_counts.columns))
            else:
                input_counts = sc.AnnData(X=sp.csr_matrix(input_counts.values),
                                       obs=pd.DataFrame(index=input_counts.index),
                                       var=pd.DataFrame(index=input_counts.columns))

                
        if sp.issparse(input_counts.X) & densify:
            input_counts.X = np.array(input_counts.X.todense())
 
        if tpm_fn is None:
            tpm = compute_tpm(input_counts)
            sc.write(self.paths['tpm'], tpm)
        elif tpm_fn.endswith('.mtx') or tpm_fn.endswith('.mtx.gz'):
            tpm_dir = os.path.dirname(tpm_fn)
            tpm = sc.read_10x_mtx(path = tpm_dir)      
            sc.write(self.paths['tpm'], tpm)        
        elif tpm_fn.endswith('.h5ad'):
            subprocess.call('cp %s %s' % (tpm_fn, self.paths['tpm']), shell=True)
            tpm = sc.read(self.paths['tpm'])
        else:
            if tpm_fn.endswith('.npz'):
                tpm = load_df_from_npz(tpm_fn)
            else:
                tpm = pd.read_csv(tpm_fn, sep='\t', index_col=0)
            
            if densify:
                tpm = sc.AnnData(X=tpm.values,
                            obs=pd.DataFrame(index=tpm.index),
                            var=pd.DataFrame(index=tpm.columns)) 
            else:
                tpm = sc.AnnData(X=sp.csr_matrix(tpm.values),
                            obs=pd.DataFrame(index=tpm.index),
                            var=pd.DataFrame(index=tpm.columns)) 

            sc.write(self.paths['tpm'], tpm)
        
        if sp.issparse(tpm.X):
            gene_tpm_mean, gene_tpm_stddev = get_mean_var(tpm.X)
            gene_tpm_stddev = gene_tpm_stddev**.5
        else:
            gene_tpm_mean = np.array(tpm.X.mean(axis=0)).reshape(-1)
            gene_tpm_stddev = np.array(tpm.X.std(axis=0, ddof=0)).reshape(-1)
            
            
        input_tpm_stats = pd.DataFrame([gene_tpm_mean, gene_tpm_stddev],
             index = ['__mean', '__std'], columns = tpm.var.index).T
        save_df_to_npz(input_tpm_stats, self.paths['tpm_stats'])
        
        if genes_file is not None:
            highvargenes = open(genes_file).read().rstrip().split('\n')
        else:
            highvargenes = None

        norm_counts = self.get_norm_counts(input_counts, tpm, num_highvar_genes=num_highvar_genes,
                                               high_variance_genes_filter=highvargenes)

        self.save_norm_counts(norm_counts)
        (replicate_params, run_params) = self.get_nmf_iter_params(ks=components, n_iter=n_iter, random_state_seed=seed,
                                                                  beta_loss=beta_loss, alpha_usage=alpha_usage,
                                                                  alpha_spectra=alpha_spectra, init=init, 
                                                                  total_workers=total_workers, use_gpu=use_gpu,
                                                                  batch_size=batch_size, max_iter=max_NMF_iter)
        self.save_nmf_iter_params(replicate_params, run_params)
        
    
    def combine(self, components=None, skip_missing_files=False):
        """
        Combine NMF iterations for the same value of K
        Parameters
        ----------
        components : list or None
            Values of K to combine iterations for. Defaults to all.

        skip_missing_files : boolean
            If True, ignore iteration files that aren't found rather than crashing. Default: False
        """

        if type(components) is int:
            ks = [components]
        elif components is None:
            run_params = load_df_from_npz(self.paths['nmf_replicate_parameters'])
            ks = sorted(set(run_params.n_components))
        else:
            ks = components

        for k in ks:
            self.combine_nmf(k, skip_missing_files=skip_missing_files)    
    
    
    
    def get_norm_counts(self, counts, tpm,
                         high_variance_genes_filter = None,
                         num_highvar_genes = None
                         ):
        """
        Parameters
        ----------

        counts : anndata.AnnData
            Scanpy AnnData object (cells x genes) containing raw counts. Filtered such that
            no genes or cells with 0 counts
        
        tpm : anndata.AnnData
            Scanpy AnnData object (cells x genes) containing tpm normalized data matching
            counts

        high_variance_genes_filter : np.array, optional (default=None)
            A pre-specified list of genes considered to be high-variance.
            Only these genes will be used during factorization of the counts matrix.
            Must match the .var index of counts and tpm.
            If set to None, high-variance genes will be automatically computed, using the
            parameters below.

        num_highvar_genes : int, optional (default=None)
            Instead of providing an array of high-variance genes, identify this many most overdispersed genes
            for filtering

        Returns
        -------

        normcounts : anndata.AnnData, shape (cells, num_highvar_genes)
            A counts matrix containing only the high variance genes and with columns (genes)normalized to unit
            variance

        """

        if high_variance_genes_filter is None:
            ## Get list of high-var genes if one wasn't provided
            if sp.issparse(tpm.X):
                (gene_counts_stats, gene_fano_params) = get_highvar_genes_sparse(tpm.X, numgenes=num_highvar_genes)  
            else:
                (gene_counts_stats, gene_fano_params) = get_highvar_genes(np.array(tpm.X), numgenes=num_highvar_genes)
                
            high_variance_genes_filter = list(tpm.var.index[gene_counts_stats.high_var.values])
                
        ## Subset out high-variance genes
        norm_counts = counts[:, high_variance_genes_filter].copy()
        norm_counts.X = norm_counts.X.astype(np.float64)

        ## Scale genes to unit variance
        if sp.issparse(tpm.X):
            sc.pp.scale(norm_counts, zero_center=False)
            if np.isnan(norm_counts.X.data).sum() > 0:
                print('Warning NaNs in normalized counts matrix')                       
        else:
            norm_counts.X /= norm_counts.X.std(axis=0, ddof=1)
            if np.isnan(norm_counts.X).sum().sum() > 0:
                print('Warning NaNs in normalized counts matrix')                    
        
        ## Save a \n-delimited list of the high-variance genes used for factorization
        with open(self.paths['nmf_genes_list'], 'w') as F:
            F.write('\n'.join(high_variance_genes_filter))

        ## Check for any cells that have 0 counts of the overdispersed genes
        zerocells = np.array(norm_counts.X.sum(axis=1)==0).reshape(-1)
        if zerocells.sum()>0:
            examples = norm_counts.obs.index[np.ravel(zerocells)]
            raise Exception('Error: %d cells have zero counts of overdispersed genes. E.g. %s. Filter those cells and re-run or adjust the number of overdispersed genes. Quitting!' % (zerocells.sum(), ', '.join(examples[:4])))
        
        return(norm_counts)

    
    def save_norm_counts(self, norm_counts):
        self._initialize_dirs()
        sc.write(self.paths['normalized_counts'], norm_counts)

        
    def get_nmf_iter_params(self, ks, n_iter = 100,
                               random_state_seed = None,
                               beta_loss = 'kullback-leibler',
                               alpha_usage=0.0, alpha_spectra=0.0,
                               init='random', total_workers=-1, 
                               use_gpu=False, batch_size=5000, 
                               max_iter=1000):
        """
        Create a DataFrame with parameters for NMF iterations.


        Parameters
        ----------
        ks : integer, or list-like.
            Number of topics (components) for factorization.
            Several values can be specified at the same time, which will be run independently.

        n_iter : integer, optional (defailt=100)
            Number of iterations for factorization. If several ``k`` are specified, this many
            iterations will be run for each value of ``k``.

        random_state_seed : int or None, optional (default=None)
            Seed for sklearn random state.
            
        alpha_usage : float, optional (default=0.0)
            Regularization parameter for NMF corresponding to alpha_W in scikit-learn

        alpha_spectra : float, optional (default=0.0)
            Regularization parameter for NMF corresponding to alpha_H in scikit-learn
        """

        if type(ks) is int:
            ks = [ks]

        # Remove any repeated k values, and order.
        k_list = sorted(set(list(ks)))

        n_runs = len(ks)* n_iter

        np.random.seed(seed=random_state_seed)
        nmf_seeds = np.random.randint(low=1, high=(2**31)-1, size=n_runs)

        replicate_params = []
        for i, (k, r) in enumerate(itertools.product(k_list, range(n_iter))):
            if not os.path.exists(self.paths['iter_spectra'] % (k, r)):
                replicate_params.append([k, r, nmf_seeds[i], False])
            else:
                replicate_params.append([k, r, nmf_seeds[i], True])
        replicate_params = pd.DataFrame(replicate_params, columns = ['n_components', 'iter', 'nmf_seed', 'completed'])
        
        n_completed = replicate_params['completed'].sum()
        if  n_completed > 0:
            message = """{n} runs already appear completed. If this is unexpected, consider
            re-initializing the cnmf object with a different run name or output directory""".format(n=n_completed)
            warnings.warn(message, UserWarning)

        _nmf_kwargs = dict(
                        alpha_W=alpha_spectra, # W, H are switched w.r.t. sklearn
                        alpha_H=alpha_usage,
                        l1_ratio_H=0.0,
                        l1_ratio_W=0.0,
                        beta_loss=beta_loss,
                        algo='mu',
                        tol=1e-4,
                        mode='online',
                        online_chunk_max_iter=max_iter,
                        online_chunk_size=batch_size,
                        init=init,
                        n_jobs=total_workers,
                        use_gpu=use_gpu
                        )
        
        ## Coordinate descent is faster than multiplicative update but only works for frobenius
        # if beta_loss == 'frobenius':
        #     _nmf_kwargs['solver'] = 'cd'

        return(replicate_params, _nmf_kwargs)
    
    
    def update_nmf_iter_params(self):
        """
        Update the replicate parameters file to indicate jobs that have already completed
        """
        _nmf_kwargs = yaml.load(open(self.paths['nmf_run_parameters']), Loader=yaml.FullLoader)
        replicate_params = load_df_from_npz(self.paths['nmf_replicate_parameters'])
        for i in replicate_params.index:
            if not os.path.exists(self.paths['iter_spectra'] % (replicate_params.at[i, 'n_components'], replicate_params.at[i, 'iter'])):
                replicate_params.at[i, 'completed'] = False
            else:
                replicate_params.at[i, 'completed'] = True
                
        remaining = (replicate_params['completed'] == False).sum()
        print('{n} NMF runs are currently incomplete'.format(n=remaining))
        
        self.save_nmf_iter_params(replicate_params, _nmf_kwargs)


    def save_nmf_iter_params(self, replicate_params, run_params):
        self._initialize_dirs()
        save_df_to_npz(replicate_params, self.paths['nmf_replicate_parameters'])
        with open(self.paths['nmf_run_parameters'], 'w') as F:
            yaml.dump(run_params, F)


    def _nmf(self, X, nmf_kwargs):
        """
        Parameters
        ----------
        X : pandas.DataFrame,
            Normalized counts dataFrame to be factorized.

        nmf_kwargs : dict,
            Arguments to be passed to ``non_negative_factorization``

        """
        # (usages, spectra, niter) = non_negative_factorization(X, **nmf_kwargs)
        if sp.issparse(X):
            X = X.toarray()
        (usages, spectra, err) = run_nmf(X, **nmf_kwargs)

        return(spectra, usages)


    # def factorize_multi_process(self, total_workers):
    #     """
    #     multiproces wrapper for nmf.factorize()
    #     factorize_multi_process() is direct wrapper around factorize to be able to launch it form mp.
    #     total_workers: int; number of workers to use.
    #     """
    #     list_args = [(x, total_workers, self) for x in range(total_workers)]
        
    #     with Pool(total_workers) as p:
            
    #         p.map(factorize_mp_signature, list_args)
    #         p.close()
    #         p.join()    
  
    
    def factorize(self,
                worker_i=0, total_workers=1, skip_completed_runs=False,
                ):
        """
        Iteratively run NMF with prespecified parameters.

        Use the `worker_i` and `total_workers` parameters for parallelization.
        
        Parameters
        ----------
        worker_i : int (default=0)
            index of worker who's jobs will be executed
            
        total_workers : int (default=1),
            total number of workers for jobs to be distributed over
            
        skip_completed_runs : boolean (default=False),
            If true, skips files that have already completed. Run self.update_nmf_iter_params() to update
            the ledger of completed runs first if setting to True.

        Generic kwargs for NMF are loaded from self.paths['nmf_run_parameters'], defaults below::

            ``non_negative_factorization`` default arguments:
                alpha=0.0
                l1_ratio=0.0
                beta_loss='kullback-leibler'
                solver='mu'
                tol=1e-4,
                max_iter=200
                regularization=None
                init='random'
                random_state, n_components are both set by the prespecified self.paths['nmf_replicate_parameters'].
        """
        run_params = load_df_from_npz(self.paths['nmf_replicate_parameters'])
        norm_counts = sc.read(self.paths['normalized_counts'])
        _nmf_kwargs = yaml.load(open(self.paths['nmf_run_parameters']), Loader=yaml.FullLoader)

        if not skip_completed_runs:
            jobs_for_this_worker = worker_filter(range(len(run_params)), worker_i, total_workers)
        else:
            jobs_for_this_worker = worker_filter(run_params.index[run_params['completed']==False],
                                                 worker_i, total_workers)
    
        for idx in jobs_for_this_worker:
            p = run_params.iloc[idx, :]
            print('[Worker %d]. Starting task %d.' % (worker_i, idx))
            _nmf_kwargs['random_state'] = p['nmf_seed']
            _nmf_kwargs['n_components'] = p['n_components']

            (spectra, usages) = self._nmf(norm_counts.X, _nmf_kwargs)
            spectra = pd.DataFrame(spectra,
                                   index=np.arange(1, _nmf_kwargs['n_components']+1),
                                   columns=norm_counts.var.index)
            save_df_to_npz(spectra, self.paths['iter_spectra'] % (p['n_components'], p['iter']))


    def combine_nmf(self, k, skip_missing_files=False, remove_individual_iterations=False):
        run_params = load_df_from_npz(self.paths['nmf_replicate_parameters'])
        print('Combining factorizations for k=%d.'%k)

        run_params_subset = run_params[run_params.n_components==k].sort_values('iter')
        combined_spectra = []

        for i,p in run_params_subset.iterrows():
            current_file = self.paths['iter_spectra'] % (p['n_components'], p['iter'])
            if not os.path.exists(current_file):
                if not skip_missing_files:
                    print('Missing file: %s, run with skip_missing=True to override' % current_file)
                    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), current_file)
                else:
                    print('Missing file: %s. Skipping.' % current_file)
            else:
                spectra = load_df_from_npz(current_file)
                spectra.index = ['iter%d_topic%d' % (p['iter'], t+1) for t in range(k)]
                combined_spectra.append(spectra)
                
        if len(combined_spectra)>0:        
            combined_spectra = pd.concat(combined_spectra, axis=0)
            save_df_to_npz(combined_spectra, self.paths['merged_spectra']%k)
        else:
            print('No spectra found for k=%d' % k)
        return combined_spectra
    
    
    def refit_usage(self, X, spectra, usage=None):
        """
        Takes an input data matrix and a fixed spectra and uses NNLS to find the optimal
        usage matrix. Generic kwargs for NMF are loaded from self.paths['nmf_run_parameters'].
        If input data are pandas.DataFrame, returns a DataFrame with row index matching X and
        columns index matching index of spectra

        Parameters
        ----------
        X : pandas.DataFrame or numpy.ndarray, cells X genes
            Non-negative expression data to fit spectra to

        spectra : pandas.DataFrame or numpy.ndarray, programs X genes
            Non-negative spectra of expression programs
        """

        refit_nmf_kwargs = yaml.load(open(self.paths['nmf_run_parameters']), Loader=yaml.FullLoader)

        beta_loss = refit_nmf_kwargs['beta_loss']

        # Choose correct loss
        if beta_loss == 'frobenius':
            beta_loss = 2
        elif beta_loss == 'kullback-leibler':
            beta_loss = 1
        elif beta_loss == 'itakura-saito':
            beta_loss = 0
        elif not (isinstance(beta_loss, int) or isinstance(beta_loss, float)):
            raise ValueError("beta_loss must be a valid value: either from ['frobenius', 'kullback-leibler', 'itakura-saito'], or a numeric value.")

        # Choose device
        device_type = 'cpu'
        if refit_nmf_kwargs['use_gpu']:
            if torch.cuda.is_available():
                device_type = 'cuda'
                print("Use GPU mode.")
            else:
                print("CUDA is not available on your machine. Use CPU mode instead.")

        # Refit usages (denoted H here)
        rf_usages = fit_H_online(
                        X,
                        spectra,
                        H_init=usage,
                        chunk_size= refit_nmf_kwargs['online_chunk_size'],
                        chunk_max_iter = refit_nmf_kwargs['online_chunk_max_iter'],
                        h_tol= 0.05,
                        l1_reg_H = refit_nmf_kwargs['l1_ratio_H'],
                        l2_reg_H = 0.0,
                        epsilon = 1e-16,
                        device = device_type
                        )

        return (rf_usages)
    
    
    def refit_spectra(self, X, usage):
        """
        Takes an input data matrix and a fixed usage matrix and uses NNLS to find the optimal
        spectra matrix. Generic kwargs for NMF are loaded from self.paths['nmf_run_parameters'].
        If input data are pandas.DataFrame, returns a DataFrame with row index matching X and
        columns index matching index of spectra

        Parameters
        ----------
        X : pandas.DataFrame or numpy.ndarray, cells X genes
            Non-negative expression data to fit spectra to

        usage : pandas.DataFrame or numpy.ndarray, cells X programs
            Non-negative spectra of expression programs
        """
        return(self.refit_usage(X.T, usage.T).T)


    def consensus(self, k, density_threshold=0.5, local_neighborhood_size=0.30, show_clustering=True,
                  build_ref=True, skip_density_and_return_after_stats=False, close_clustergram_fig=False,
                  refit_usage=True, normalize_tpm_spectra=False, norm_counts=None):
        """
        Obtain consensus estimates of spectra and usages from a cNMF run and output a clustergram of
        the consensus matrix. Assumes prepare, factorize, and combine steps have already been run.


        Parameters
        ----------
        k : int
            Number of programs (must be within the k values specified in previous steps)

        density_threshold : float (default: 0.5)
            Threshold for filtering outlier spectra. 2.0 or greater applies no filter.
            
        local_neighborhood_size : float (default: 0.3)
            Determines number of neighbors to use for calculating KNN distance as local_neighborhood_size X n_iters

        show_clustering : boolean (default=False)
            If True, generates the consensus clustergram filter
            
        build_ref : boolean (default=True)
            If True, generates reference spectra for use in starCAT

        skip_density_and_return_after_stats : boolean (default=False)
            True when running k_selection_plot to compute stability and error for input parameters without computing
            consensus spectra and usages
            
        close_clustergram_fig : boolean (default=False)
            If True, closes the clustergram figure from output after saving the image to a file
            
        refit_usage : boolean (default=True)
            If True, refit the usage matrix one final time after finalizing the spectra_tpm matrix. Done by regressing 
            the tpm matrix against the tpm_spectra including only high-variance genes and with both the tpm matrix
            and tpm_spectra normalized by the standard deviations of the genes in tpm scale.
            
        normalize_tpm_spectra : boolean (default=False)
            If True, renormalizes the tpm_spectra to sum to 1e6 for each program. This is done before refitting usages.
            If not used, the tpm_spectra are exactly as calcuated when refitting the usage matrix against the tpm matrix
            and typically will not sum to the same value for each program.
            
        norm_counts : AnnData (default=None)
            Speed up calculation of k_selection_plot by avoiding reloading norm_counts for each K. Should not be used by
            most users
        """
        
        
        merged_spectra = load_df_from_npz(self.paths['merged_spectra']%k)
        if norm_counts is None:
            norm_counts = sc.read(self.paths['normalized_counts'])

        density_threshold_str = str(density_threshold)
        if skip_density_and_return_after_stats:
            density_threshold_str = '2'
        density_threshold_repl = density_threshold_str.replace('.', '_')
        n_neighbors = int(local_neighborhood_size * merged_spectra.shape[0]/k)

        # Rescale topics such to length of 1.
        l2_spectra = (merged_spectra.T/np.sqrt((merged_spectra**2).sum(axis=1))).T

        if not skip_density_and_return_after_stats:
            # Compute the local density matrix (if not previously cached)
            topics_dist = None
            if os.path.isfile(self.paths['local_density_cache'] % k):
                local_density = load_df_from_npz(self.paths['local_density_cache'] % k)
            else:
                #   first find the full distance matrix
                topics_dist = euclidean_distances(l2_spectra.values)
                #   partition based on the first n neighbors
                partitioning_order  = np.argpartition(topics_dist, n_neighbors+1)[:, :n_neighbors+1]
                #   find the mean over those n_neighbors (excluding self, which has a distance of 0)
                distance_to_nearest_neighbors = topics_dist[np.arange(topics_dist.shape[0])[:, None], partitioning_order]
                local_density = pd.DataFrame(distance_to_nearest_neighbors.sum(1)/(n_neighbors),
                                             columns=['local_density'],
                                             index=l2_spectra.index)
                save_df_to_npz(local_density, self.paths['local_density_cache'] % k)
                del(partitioning_order)
                del(distance_to_nearest_neighbors)

            density_filter = local_density.iloc[:, 0] < density_threshold
            l2_spectra = l2_spectra.loc[density_filter, :]
            if l2_spectra.shape[0] == 0:
                raise RuntimeError("Zero components remain after density filtering. Consider increasing density threshold")

        kmeans_model = KMeans(n_clusters=k, n_init=10, random_state=1)
        kmeans_model.fit(l2_spectra)
        kmeans_cluster_labels = pd.Series(kmeans_model.labels_+1, index=l2_spectra.index)

        # Find median usage for each gene across cluster
        median_spectra = l2_spectra.groupby(kmeans_cluster_labels).median()

        # Normalize median spectra to probability distributions.
        median_spectra = (median_spectra.T/median_spectra.sum(1)).T

        # Obtain reconstructed count matrix by re-fitting usage and computing dot product: usage.dot(spectra)
        rf_usages = self.refit_usage(norm_counts.X, median_spectra)
        rf_usages = pd.DataFrame(rf_usages, index=norm_counts.obs.index, columns=median_spectra.index)     
        
        if skip_density_and_return_after_stats:
            silhouette = silhouette_score(l2_spectra.values, kmeans_cluster_labels, metric='euclidean')
            
            # Compute prediction error as a frobenius norm
            rf_pred_norm_counts = rf_usages.dot(median_spectra)        
            if sp.issparse(norm_counts.X):
                prediction_error = ((norm_counts.X.todense() - rf_pred_norm_counts)**2).sum().sum()
            else:
                prediction_error = ((norm_counts.X - rf_pred_norm_counts)**2).sum().sum()    
                
            consensus_stats = pd.DataFrame([k, density_threshold, silhouette,  prediction_error],
                    index = ['k', 'local_density_threshold', 'silhouette', 'prediction_error'],
                    columns = ['stats'])

            return(consensus_stats)           
        
        # Re-order usage by total contribution
        norm_usages = rf_usages.div(rf_usages.sum(axis=1), axis=0)      
        reorder = norm_usages.sum(axis=0).sort_values(ascending=False)
        rf_usages = rf_usages.loc[:, reorder.index]
        norm_usages = norm_usages.loc[:, reorder.index]
        median_spectra = median_spectra.loc[reorder.index, :]
        rf_usages.columns = np.arange(1, rf_usages.shape[1]+1)
        norm_usages.columns = rf_usages.columns
        median_spectra.index = rf_usages.columns
        
        # Convert spectra to TPM units, and obtain results for all genes by running last step of NMF
        # with usages fixed and TPM as the input matrix
        tpm = sc.read(self.paths['tpm'])
        tpm_stats = load_df_from_npz(self.paths['tpm_stats'])
        spectra_tpm = self.refit_spectra(tpm.X, norm_usages.astype(tpm.X.dtype))
        spectra_tpm = pd.DataFrame(spectra_tpm, index=rf_usages.columns, columns=tpm.var.index)
        if normalize_tpm_spectra:
            spectra_tpm = spectra_tpm.div(spectra_tpm.sum(axis=1), axis=0) * 1e6
                    
        # Convert spectra to Z-score units by fitting OLS regression of the Z-scored TPM against GEP usage
        usage_coef = efficient_ols_all_cols(rf_usages.values, tpm.X, normalize_y=True)
        usage_coef = pd.DataFrame(usage_coef, index=rf_usages.columns, columns=tpm.var.index)
        
        if refit_usage:
            ## Re-fitting usage a final time on std-scaled HVG TPM seems to
            ## increase accuracy on simulated data
            hvgs = open(self.paths['nmf_genes_list']).read().split('\n')
            norm_tpm = tpm[:, hvgs]
            if sp.issparse(norm_tpm.X):
                sc.pp.scale(norm_tpm, zero_center=False)                       
            else:
                norm_tpm.X /= norm_tpm.X.std(axis=0, ddof=1)
                
            spectra_tpm_rf = spectra_tpm.loc[:,hvgs]

            spectra_tpm_rf = spectra_tpm_rf.div(tpm_stats.loc[hvgs, '__std'], axis=1)
            rf_usages = self.refit_usage(norm_tpm.X, spectra_tpm_rf.astype(norm_tpm.X.dtype))
            rf_usages = pd.DataFrame(rf_usages, index=norm_counts.obs.index, columns=spectra_tpm_rf.index)                                                                  
               
        save_df_to_npz(median_spectra, self.paths['consensus_spectra']%(k, density_threshold_repl))
        save_df_to_npz(rf_usages, self.paths['consensus_usages']%(k, density_threshold_repl))
        #save_df_to_npz(consensus_stats, self.paths['consensus_stats']%(k, density_threshold_repl))
        save_df_to_text(median_spectra, self.paths['consensus_spectra__txt']%(k, density_threshold_repl))
        save_df_to_text(rf_usages, self.paths['consensus_usages__txt']%(k, density_threshold_repl))
        save_df_to_npz(spectra_tpm, self.paths['gene_spectra_tpm']%(k, density_threshold_repl))
        save_df_to_text(spectra_tpm, self.paths['gene_spectra_tpm__txt']%(k, density_threshold_repl))
        save_df_to_npz(usage_coef, self.paths['gene_spectra_score']%(k, density_threshold_repl))
        save_df_to_text(usage_coef, self.paths['gene_spectra_score__txt']%(k, density_threshold_repl))
        if show_clustering:
            if topics_dist is None:
                topics_dist = euclidean_distances(l2_spectra.values)
                # (l2_spectra was already filtered using the density filter)
            else:
                # (but the previously computed topics_dist was not!)
                topics_dist = topics_dist[density_filter.values, :][:, density_filter.values]


            spectra_order = []
            for cl in sorted(set(kmeans_cluster_labels)):

                cl_filter = kmeans_cluster_labels==cl

                if cl_filter.sum() > 1:
                    cl_dist = squareform(topics_dist[cl_filter, :][:, cl_filter], checks=False)
                    cl_dist[cl_dist < 0] = 0 #Rarely get floating point arithmetic issues
                    cl_link = linkage(cl_dist, 'average')
                    cl_leaves_order = leaves_list(cl_link)

                    spectra_order += list(np.where(cl_filter)[0][cl_leaves_order])
                else:
                    ## Corner case where a component only has one element
                    spectra_order += list(np.where(cl_filter)[0])


            from matplotlib import gridspec
            import matplotlib.pyplot as plt

            width_ratios = [0.5, 9, 0.5, 4, 1]
            height_ratios = [0.5, 9]
            fig = plt.figure(figsize=(sum(width_ratios), sum(height_ratios)))
            gs = gridspec.GridSpec(len(height_ratios), len(width_ratios), fig,
                                    0.01, 0.01, 0.98, 0.98,
                                   height_ratios=height_ratios,
                                   width_ratios=width_ratios,
                                   wspace=0, hspace=0)

            dist_ax = fig.add_subplot(gs[1,1], xscale='linear', yscale='linear',
                                      xticks=[], yticks=[],xlabel='', ylabel='',
                                      frameon=True)

            D = topics_dist[spectra_order, :][:, spectra_order]
            dist_im = dist_ax.imshow(D, interpolation='none', cmap='viridis',
                                     aspect='auto', rasterized=True)

            left_ax = fig.add_subplot(gs[1,0], xscale='linear', yscale='linear', xticks=[], yticks=[],
                xlabel='', ylabel='', frameon=True)
            left_ax.imshow(kmeans_cluster_labels.values[spectra_order].reshape(-1, 1),
                            interpolation='none', cmap='Spectral', aspect='auto',
                            rasterized=True)


            top_ax = fig.add_subplot(gs[0,1], xscale='linear', yscale='linear', xticks=[], yticks=[],
                xlabel='', ylabel='', frameon=True)
            top_ax.imshow(kmeans_cluster_labels.values[spectra_order].reshape(1, -1),
                              interpolation='none', cmap='Spectral', aspect='auto',
                                rasterized=True)


            hist_gs = gridspec.GridSpecFromSubplotSpec(3, 1, subplot_spec=gs[1, 3],
                                   wspace=0, hspace=0)

            hist_ax = fig.add_subplot(hist_gs[0,0], xscale='linear', yscale='linear',
                xlabel='', ylabel='', frameon=True, title='Local density histogram')
            hist_ax.hist(local_density.values, bins=np.linspace(0, 1, 50))
            hist_ax.yaxis.tick_right()

            xlim = hist_ax.get_xlim()
            ylim = hist_ax.get_ylim()
            if density_threshold < xlim[1]:
                hist_ax.axvline(density_threshold, linestyle='--', color='k')
                hist_ax.text(density_threshold  + 0.02, ylim[1] * 0.95, 'filtering\nthreshold\n\n', va='top')
            hist_ax.set_xlim(xlim)
            hist_ax.set_xlabel('Mean distance to k nearest neighbors\n\n%d/%d (%.0f%%) spectra above threshold\nwere removed prior to clustering'%(sum(~density_filter), len(density_filter), 100*(~density_filter).mean()))
            
            ## Add colorbar
            cbar_gs = gridspec.GridSpecFromSubplotSpec(8, 1, subplot_spec=hist_gs[1, 0],
                                   wspace=0, hspace=0)
            cbar_ax = fig.add_subplot(cbar_gs[4,0], xscale='linear', yscale='linear',
                xlabel='', ylabel='', frameon=True, title='Euclidean Distance')
            vmin = D.min().min()
            vmax = D.max().max()
            fig.colorbar(dist_im, cax=cbar_ax,
            ticks=np.linspace(vmin, vmax, 3),
            orientation='horizontal')
            
            
            #hist_ax.hist(local_density.values, bins=np.linspace(0, 1, 50))
            #hist_ax.yaxis.tick_right()            

            fig.savefig(self.paths['clustering_plot']%(k, density_threshold_repl), dpi=250)
            if close_clustergram_fig:
                plt.close(fig)
                
        if build_ref:
            self.build_reference(k, density_threshold)
                
                
    def build_reference(self, k, density_threshold=0.5, target_sum=1e6):
        '''
        Builds reference GEPs for use with starCAT using the results from "consensus" step. 

        Parameters
        ----------
        k : int
            Number of programs (must be within the k values specified in previous steps)

        density_threshold : float (default: 0.5)
            Threshold for filtering outlier spectra. 2.0 or greater applies no filter.
        '''
        density_threshold_repl = str(density_threshold).replace('.', '_')
        tpmfn = self.paths['gene_spectra_tpm__txt'] % (k, density_threshold_repl)
        spectra_tpm = pd.read_csv(tpmfn, index_col = 0, sep = '\t')
        hvgs = open(self.paths['nmf_genes_list']).read().split('\n')
        
        tpm_stats = load_df_from_npz(self.paths['tpm_stats'])
        tpm_stats.index = spectra_tpm.columns
        
        # Renormalize TPM spectra
        spectra_tpm_renorm = spectra_tpm.copy()
        spectra_tpm_renorm = spectra_tpm_renorm.div(spectra_tpm_renorm.sum(axis=1), axis=0)*target_sum

        # Var-norm TPM spectra
        spectra_tpm_varnorm = spectra_tpm_renorm.div(tpm_stats['__std'])

        ref_spectra = spectra_tpm_varnorm[hvgs].copy()
        ref_spectra.index = 'GEP' + ref_spectra.index.astype('str')
        
        save_df_to_npz(ref_spectra, self.paths['starcat_spectra']%(k, density_threshold_repl))
        save_df_to_text(ref_spectra, self.paths['starcat_spectra__txt']%(k, density_threshold_repl))


    def k_selection_plot(self, close_fig=False):
        '''
        Borrowed from Alexandrov Et Al. 2013 Deciphering Mutational Signatures
        publication in Cell Reports
        '''
        run_params = load_df_from_npz(self.paths['nmf_replicate_parameters'])
        stats = []
        norm_counts = sc.read(self.paths['normalized_counts'])
        for k in sorted(set(run_params.n_components)):
            stats.append(self.consensus(k, skip_density_and_return_after_stats=True,
                                        show_clustering=False, close_clustergram_fig=True,
                                        norm_counts=norm_counts).stats)

        stats = pd.DataFrame(stats)
        stats.reset_index(drop = True, inplace = True)

        save_df_to_npz(stats, self.paths['k_selection_stats'])

        fig = plt.figure(figsize=(6, 4))
        ax1 = fig.add_subplot(111)
        ax2 = ax1.twinx()


        ax1.plot(stats.k, stats.silhouette, 'o-', color='b')
        ax1.set_ylabel('Stability', color='b', fontsize=15)
        for tl in ax1.get_yticklabels():
            tl.set_color('b')
        #ax1.set_xlabel('K', fontsize=15)

        ax2.plot(stats.k, stats.prediction_error, 'o-', color='r')
        ax2.set_ylabel('Error', color='r', fontsize=15)
        for tl in ax2.get_yticklabels():
            tl.set_color('r')

        ax1.set_xlabel('Number of Components', fontsize=15)
        ax1.grid('on')
        plt.tight_layout()
        fig.savefig(self.paths['k_selection_plot'], dpi=250)
        if close_fig:
            plt.close(fig)
            
            
    def load_results(self, K, density_threshold, n_top_genes=100, norm_usage = True):
        """
        Loads normalized usages and gene_spectra_scores for a given choice of K and 
        local_density_threshold for the cNMF run. Additionally returns a DataFrame of
        the top genes linked to each program
        
        Parameters
        ----------
        K : int
            Number of programs (must be within the k values specified in previous steps)

        density_threshold : float
            Threshold for filtering outlier spectra (must be within the values specified in consensus step)

        n_top_genes : integer, optional (default=100)
            Number of top genes per program to return

        norm_usage : boolean, optional (default=True)
            If True, normalize cNMF usages to sum to 1
        
        Returns
        ----------
        usage - cNMF usages (cells X K)
        spectra_scores - Z-score coeffecients for each program (K x genes) with high values cooresponding
                    to better marker genes
        spectra_tpm - Coeffecients for contribution of each gene to each program (K x genes) in TPM units
        top_genes - ranked list of marker genes per GEP (n_top_genes X K)
        """
        scorefn = self.paths['gene_spectra_score__txt'] % (K, str(density_threshold).replace('.', '_'))
        tpmfn = self.paths['gene_spectra_tpm__txt'] % (K, str(density_threshold).replace('.', '_'))
        usagefn = self.paths['consensus_usages__txt'] % (K, str(density_threshold).replace('.', '_'))
        spectra_scores = pd.read_csv(scorefn, sep='\t', index_col=0).T
        spectra_tpm = pd.read_csv(tpmfn, sep='\t', index_col=0).T

        usage = pd.read_csv(usagefn, sep='\t', index_col=0)
        
        if norm_usage:
            usage = usage.div(usage.sum(axis=1), axis=0)
        
        try:
            usage.columns = [int(x) for x in usage.columns]
        except:
            print('Usage matrix columns include non integer values')
    
        top_genes = []
        for gep in spectra_scores.columns:
            top_genes.append(list(spectra_scores.sort_values(by=gep, ascending=False).index[:n_top_genes]))
        
        top_genes = pd.DataFrame(top_genes, index=spectra_scores.columns).T
        return(usage, spectra_scores, spectra_tpm, top_genes)


def main():
    """
    Example commands:

        output_dir="./cnmf_test/"


        python cnmf.py prepare --output-dir $output_dir \
           --name test --counts ./cnmf_test/test_data.df.npz \
           -k 6 7 8 9 --n-iter 5

        python cnmf.py factorize  --name test --output-dir $output_dir

        THis can be parallelized as such:

        python cnmf.py factorize  --name test --output-dir $output_dir --total-workers 2 --worker-index WORKER_INDEX (where worker_index starts with 0)

        python cnmf.py combine  --name test --output-dir $output_dir

        python cnmf.py consensus  --name test --output-dir $output_dir

    """

    import sys, argparse
    parser = argparse.ArgumentParser()

    parser.add_argument('command', type=str, choices=['prepare', 'factorize', 'combine', 'consensus', 'k_selection_plot'])
    parser.add_argument('--name', type=str, help='[all] Name for analysis. All output will be placed in [output-dir]/[name]/...', nargs='?', default='cNMF')
    parser.add_argument('--output-dir', type=str, help='[all] Output directory. All output will be placed in [output-dir]/[name]/...', nargs='?', default='.')
    parser.add_argument('-c', '--counts', type=str, help='[prepare] Input (cell x gene) counts matrix as .h5ad, .mtx, df.npz, or tab delimited text file')
    parser.add_argument('-k', '--components', type=int, help='[prepare] Numper of components (k) for matrix factorization. Several can be specified with "-k 8 9 10"', nargs='+')
    parser.add_argument('-n', '--n-iter', type=int, help='[prepare] Number of factorization replicates', default=100)
    parser.add_argument('--total-workers', type=int, help='[all] Total number of workers to distribute jobs to', default=-1)
    parser.add_argument('--use_gpu', action='store_true', help='[prepare] Whether to use GPU.', default=False)
    parser.add_argument('--seed', type=int, help='[prepare] Seed for pseudorandom number generation', default=None)
    parser.add_argument('--genes-file', type=str, help='[prepare] File containing a list of genes to include, one gene per line. Must match column labels of counts matrix.', default=None)
    parser.add_argument('--numgenes', type=int, help='[prepare] Number of high variance genes to use for matrix factorization.', default=2000)
    parser.add_argument('--tpm', type=str, help='[prepare] Pre-computed (cell x gene) TPM values as df.npz or tab separated txt file. If not provided TPM will be calculated automatically', default=None)
    parser.add_argument('--max-nmf-iter', type=int, help='[prepare] Max number of iterations per individual NMF run (default 1000)', default=1000)
    parser.add_argument('--beta-loss', type=str, choices=['frobenius', 'kullback-leibler', 'itakura-saito'], help='[prepare] Loss function for NMF (default frobenius)', default='frobenius')
    parser.add_argument('--init', type=str, choices=['random', 'nndsvd'], help='[prepare] Initialization algorithm for NMF (default random)', default='random')
    parser.add_argument('--densify', dest='densify', help='[prepare] Treat the input data as non-sparse (default False)', action='store_true', default=False)
    parser.add_argument('--batch_size', type=int, help='[prepare] Size of batch for online NMF learning.', default=5000) 
    # parser.add_argument('--worker-index', type=int, help='[factorize] Index of current worker (the first worker should have index 0)', default=0)
    parser.add_argument('--skip-completed-runs', action='store_true', help='[factorize] Skip previously completed runs. Must re-run prepare first to update completed runs', default=False)
    parser.add_argument('--local-density-threshold', type=float, help='[consensus] Threshold for the local density filtering. This string must convert to a float >0 and <=2', default=0.5)
    parser.add_argument('--local-neighborhood-size', type=float, help='[consensus] Fraction of the number of replicates to use as nearest neighbors for local density filtering', default=0.30)
    parser.add_argument('--show-clustering', dest='show_clustering', help='[consensus] Produce a clustergram figure summarizing the spectra clustering', action='store_true')
    parser.add_argument('--build-reference', dest='build_reference', help='[consensus] Generates a reference spectra for use in starCAT', action='store_true', default=True)

    
    args = parser.parse_args()

    cnmf_obj = cNMF(output_dir=args.output_dir, name=args.name)
    
    if args.command == 'prepare':
        cnmf_obj.prepare(args.counts, components=args.components, n_iter=args.n_iter, densify=args.densify,
                         tpm_fn=args.tpm, seed=args.seed, beta_loss=args.beta_loss, max_NMF_iter=args.max_nmf_iter,
                         num_highvar_genes=args.numgenes, genes_file=args.genes_file, init=args.init, total_workers=args.total_workers,
                         use_gpu=args.use_gpu, batch_size=args.batch_size)

    elif args.command == 'factorize':
        cnmf_obj.factorize(skip_completed_runs=args.skip_completed_runs)

    elif args.command == 'combine':
        cnmf_obj.combine(components=args.components)

    elif args.command == 'consensus':
        run_params = load_df_from_npz(cnmf_obj.paths['nmf_replicate_parameters'])

        if type(args.components) is int:
            ks = [args.components]
        elif args.components is None:
            ks = sorted(set(run_params.n_components))
        else:
            ks = args.components

        for k in ks:
            merged_spectra = load_df_from_npz(cnmf_obj.paths['merged_spectra']%k)
            cnmf_obj.consensus(k, args.local_density_threshold, args.local_neighborhood_size, args.show_clustering,
                               args.build_reference, close_clustergram_fig=True)

    elif args.command == 'k_selection_plot':
        cnmf_obj.k_selection_plot(close_fig=True)


if __name__=="__main__":
    main()
