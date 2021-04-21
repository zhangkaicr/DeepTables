# -*- coding:utf-8 -*-

import collections
import copy
import hashlib
import os
import pickle
import time
from functools import partial

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import make_pipeline

from hypernets.tabular import sklearn_ex as skex
from .config import ModelConfig
from .metainfo import CategoricalColumn, ContinuousColumn, VarLenCategoricalColumn
from ..utils import dt_logging, consts, fs, infer_task_type, hash_data

logger = dt_logging.get_logger(__name__)


class AbstractPreprocessor:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.labels_ = None
        self.task_ = None

    @property
    def pos_label(self):
        if self.labels_ is not None and len(self.labels_) == 2:
            return self.labels_[1]
        else:
            return None

    @property
    def labels(self):
        return self.labels_

    @property
    def task(self):
        return self.task_

    @property
    def signature(self):
        repr = f'''{self.config.auto_imputation}|
{self.config.auto_encode_label}|
{self.config.auto_discrete}|
{self.config.apply_gbm_features}|
{self.config.task}|
{self.config.cat_exponent}|
{self.config.exclude_columns}|
{self.config.categorical_columns}|
{self.config.auto_categorize}|
{self.config.cat_remain_numeric}|
{self.config.auto_discard_unique}|
{self.config.gbm_params}|
{self.config.gbm_feature_type}|
{self.config.fixed_embedding_dim}|
{self.config.embeddings_output_dim}'''
        sign = hashlib.md5(repr.encode('utf-8')).hexdigest()
        return sign

    def get_X_y_signature(self, X, y):
        # repr = ''
        # if X is not None:
        #     if isinstance(X, list):
        #         repr += f'X len({len(X)})|'
        #     if hasattr(X, 'shape'):
        #         repr += f'X shape{X.shape}|'
        #     if hasattr(X, 'dtypes'):
        #         repr += f'x.dtypes({list(X.dtypes)})|'
        #
        # if y is not None:
        #     if isinstance(y, list):
        #         repr += f'y len({len(y)})|'
        #     if hasattr(y, 'shape'):
        #         repr += f'y shape{y.shape}|'
        #
        #     if hasattr(y, 'dtype'):
        #         repr += f'y.dtype({y.dtype})|'
        #
        # sign = hashlib.md5(repr.encode('utf-8')).hexdigest()
        sign = hash_data([X, y])
        return sign

    def fit_transform(self, X, y, copy_data=True):
        raise NotImplementedError

    def transform_X(self, X, copy_data=True):
        raise NotImplementedError

    def transform_y(self, y, copy_data=True):
        raise NotImplementedError

    def transform(self, X, y, copy_data=True):
        raise NotImplementedError

    def inverse_transform_y(self, y_indicator):
        raise NotImplementedError

    def get_categorical_columns(self):
        raise NotImplementedError

    def get_continuous_columns(self):
        raise NotImplementedError

    # def save(self, filepath):
    #     raise NotImplementedError
    #
    # @staticmethod
    # def load(filepath):
    #     raise NotImplementedError


class DefaultPreprocessor(AbstractPreprocessor):
    def __init__(self, config: ModelConfig, cache_home=None, use_cache=False):
        super().__init__(config)
        self.reset()
        self.X_types = None
        self.y_type = None
        self.cache_dir = self._prepare_cache_dir(cache_home)
        self.use_cache = use_cache

    @property
    def transformers(self):
        return skex

    def reset(self):
        self.metainfo = None
        self.categorical_columns = None
        self.var_len_categorical_columns = None
        self.continuous_columns = None
        self.y_lable_encoder = None
        self.X_transformers = collections.OrderedDict()

    def _validate_fit_transform(self, X, y):
        if X is None:
            raise ValueError(f'X cannot be none.')
        if y is None:
            raise ValueError(f'y cannot be none.')

        X_shape = self._get_shape(X)
        y_shape = self._get_shape(y)

        if len(X_shape) != 2:
            raise ValueError(f'X must be a 2D datasets.')
        # if len(y_shape) != 1:
        #    raise ValueError(f'y must be a 1D datasets.')
        if X_shape[0] != y_shape[0]:
            raise ValueError(f"The number of samples of X and y must be the same. X.shape:{X.shape}, y.shape{y.shape}")

        y_df = pd.DataFrame(y)
        if y_df.isnull().sum().sum() > 0:
            raise ValueError("Missing values in y.")

    def _copy(self, obj):
        return copy.deepcopy(obj)

    def _get_shape(self, obj):
        return obj.shape

    def _nunique(self, y):
        return y.nunique()

    def _prepare_X(self, X):
        if not isinstance(X, pd.DataFrame):
            X = pd.DataFrame(X)
        if len(set(X.columns)) != len(list(X.columns)):
            cols = [item for item, count in collections.Counter(X.columns).items() if count > 1]
            raise ValueError(f'Columns with duplicate names in X: {cols}')
        if X.columns.dtype != 'object':
            X.columns = ['x_' + str(c) for c in X.columns]
            logger.warn(f"Column index of X has been converted: {X.columns}")
        return X

    def fit_transform(self, X, y, copy_data=True):
        sign = self.get_X_y_signature(X, y)
        if self.use_cache:
            logger.info('Try to load (X, y) from cache')
            X_t, y_t = self.get_transformed_X_y_from_cache(sign)
            if X_t is not None and y_t is not None:
                if self.load_transformers_from_cache():
                    return X_t, y_t
            else:
                logger.info('Load failed')

        start = time.time()
        self.reset()

        self._validate_fit_transform(X, y)
        if copy:
            X = self._copy(X)
            y = self._copy(y)

        y = self.fit_transform_y(y)
        X = self._prepare_X(X)
        X = self._prepare_features(X)

        if self.config.auto_imputation:
            X = self._imputation(X)
        if self.config.auto_encode_label:
            X = self._categorical_encoding(X)
        if self.config.auto_discrete:
            X = self._discretization(X)
        if self.config.apply_gbm_features and y is not None:
            X = self._apply_gbm_features(X, y)
        var_len_categorical_columns = self.config.var_len_categorical_columns
        if var_len_categorical_columns is not None and len(var_len_categorical_columns) > 0:
            X = self._var_len_encoder(X, var_len_categorical_columns)

        self.X_transformers['last'] = self.transformers.PassThroughEstimator()

        cat_cols = self.get_categorical_columns()
        cont_cols = self.get_continuous_columns()
        if len(cat_cols) > 0:
            X[cat_cols] = X[cat_cols].astype('category')
        if len(cont_cols) > 0:
            X[cont_cols] = X[cont_cols].astype('float')

        logger.info(f'fit_transform taken {time.time() - start}s')

        if self.use_cache:
            logger.info('Put (X, y) into cache')
            self.save_transformed_X_y_to_cache(sign, X, y)
            self.save_transformers_to_cache()
        return X, y

    def fit_transform_y(self, y):
        if self.config.task == consts.TASK_AUTO:
            self.task_, self.labels_ = infer_task_type(y)
        else:
            self.task_ = self.config.task

        if self.task_ in [consts.TASK_BINARY, consts.TASK_MULTICLASS]:
            self.y_lable_encoder = self.transformers.LabelEncoder()
            y = self.y_lable_encoder.fit_transform(y)
            self.labels_ = self.y_lable_encoder.classes_
        elif self.task_ == consts.TASK_MULTILABEL:
            self.labels_ = list(range(y.shape[-1]))
        else:
            self.labels_ = []
        return y

    def transform(self, X, y, copy_data=True):
        sign = self.get_X_y_signature(X, y)
        if self.use_cache:
            logger.info('Try to load (X, y) from cache')
            X_t, y_t = self.get_transformed_X_y_from_cache(sign)
            if X_t is not None and y_t is not None:
                return X_t, y_t
            else:
                logger.info('Load failed')

        X_t = self.transform_X(X, copy_data)
        y_t = self.transform_y(y, copy_data)

        cat_cols = self.get_categorical_columns()
        cont_cols = self.get_continuous_columns()
        if len(cat_cols) > 0:
            X_t[cat_cols] = X_t[cat_cols].astype('category')
        if len(cont_cols) > 0:
            X_t[cont_cols] = X_t[cont_cols].astype('float')

        if self.use_cache:
            logger.info('Put (X, y) into cache')
            self.save_transformed_X_y_to_cache(sign, X_t, y_t)

        return X_t, y_t

    def transform_y(self, y, copy_data=True):
        logger.info("Transform [y]...")
        start = time.time()
        if copy_data:
            y = self._copy(y)
        if self.y_lable_encoder is not None:
            y = self.y_lable_encoder.transform(y)
        logger.info(f'transform_y taken {time.time() - start}s')
        y = np.array(y)
        return y

    def transform_X(self, X, copy_data=True):
        start = time.time()
        logger.info("Transform [X]...")
        if copy_data:
            X = self._copy(X)
        X = self._prepare_X(X)
        steps = [step for step in self.X_transformers.values()]
        pipeline = make_pipeline(*steps)
        X_t = pipeline.transform(X)
        logger.info(f'transform_X taken {time.time() - start}s')
        return X_t

    def inverse_transform_y(self, y_indicator):
        if self.y_lable_encoder is not None:
            return self.y_lable_encoder.inverse_transform(y_indicator)
        else:
            return y_indicator

    def _prepare_features(self, X):
        start = time.time()

        logger.info(f'Preparing features...')
        num_vars = []
        convert2cat_vars = []
        cat_vars = []
        excluded_vars = []

        if self.config.cat_exponent >= 1:
            raise ValueError(f'"cat_exponent" must be less than 1, not {self.config.cat_exponent} .')

        var_len_categorical_columns = self.config.var_len_categorical_columns
        var_len_column_names = []
        if var_len_categorical_columns is not None and len(var_len_categorical_columns) > 0:
            # check items
            for v in var_len_categorical_columns:
                if not isinstance(v, (tuple, list)) or len(v) != 3:
                    raise ValueError("Var len column config should be a tuple 3.")
                else:
                    var_len_column_names.append(v[0])
            var_len_col_sep_dict = {v[0]: v[1] for v in var_len_categorical_columns}
            var_len_col_pooling_strategy_dict = {v[0]: v[2] for v in var_len_categorical_columns}
        else:
            var_len_col_sep_dict = {}
            var_len_col_pooling_strategy_dict = {}

        X_shape = self._get_shape(X)
        unique_upper_limit = round(X_shape[0] ** self.config.cat_exponent)
        for c in X.columns:
            nunique = self._nunique(X[c])  # X[c].nunique()
            dtype = str(X[c].dtype)

            if nunique <= 1 and self.config.auto_discard_unique:
                continue

            if c in self.config.exclude_columns:
                excluded_vars.append((c, dtype, nunique))
                continue

            # handle var len feature
            if c in var_len_column_names:
                self.__append_var_len_categorical_col(c, nunique, var_len_col_sep_dict[c],
                                                      var_len_col_pooling_strategy_dict[c])
                continue

            if self.config.categorical_columns is not None and isinstance(self.config.categorical_columns, list):
                if c in self.config.categorical_columns:
                    cat_vars.append((c, dtype, nunique))
                else:
                    if np.issubdtype(dtype, np.number):
                        num_vars.append((c, dtype, nunique))
                    else:
                        print(
                            f'Column [{c}] has been discarded. It is not numeric and not in [config.categorical_columns].')
            else:
                if dtype == 'object' or dtype == 'category' or dtype == 'bool':
                    cat_vars.append((c, dtype, nunique))
                elif self.config.auto_categorize and nunique < unique_upper_limit:
                    convert2cat_vars.append((c, dtype, nunique))
                else:
                    num_vars.append((c, dtype, nunique))

        if len(convert2cat_vars) > 0:
            cat_columns = [c for c, d, n in convert2cat_vars]
            ce = self.transformers.CategorizeEncoder(cat_columns, self.config.cat_remain_numeric)
            X = ce.fit_transform(X)
            self.X_transformers['categorize'] = ce
            if self.config.cat_remain_numeric:
                cat_vars = cat_vars + ce.new_columns
                num_vars = num_vars + convert2cat_vars
            else:
                cat_vars = cat_vars + convert2cat_vars

        logger.debug(f'{len(cat_vars)} categorical variables and {len(num_vars)} continuous variables found. '
                     f'{len(convert2cat_vars)} of them are from continuous to categorical.')
        self.__append_categorical_cols([(c[0], c[2] + 2) for c in cat_vars])
        self.__append_continuous_cols([c[0] for c in num_vars], consts.INPUT_PREFIX_NUM + 'all')
        print(f'Preparing features taken {time.time() - start}s')
        return X

    def _imputation(self, X):
        start = time.time()
        logger.info('Data imputation...')
        continuous_vars = self.get_continuous_columns()
        categorical_vars = self.get_categorical_columns()
        var_len_categorical_vars = self.get_var_len_categorical_columns()

        transformers = [
            ('continuous',
             self.transformers.SimpleImputer(missing_values=np.nan, strategy='mean'),
             continuous_vars)
        ]

        obj_cats = []
        num_cats = []
        for c in categorical_vars + var_len_categorical_vars:
            dtype = str(X[c].dtype)
            if dtype.startswith('obj') or dtype.startswith('str'):
                obj_cats.append(c)
            else:
                num_cats.append(c)

        if len(obj_cats) > 0:
            transformers.append(
                ('categorical_obj',
                 self.transformers.SimpleImputer(missing_values=np.nan, strategy='constant', fill_value=''),
                 obj_cats)
            )
        if len(num_cats) > 0:
            transformers.append(
                ('categorical_num',
                 self.transformers.SimpleImputer(missing_values=np.nan, strategy='constant', fill_value=0),
                 num_cats)
            )

        if hasattr(self.transformers, 'ColumnTransformer'):
            ct = self.transformers.ColumnTransformer(transformers)
        else:
            ct = ColumnTransformer(transformers)

        columns = continuous_vars + obj_cats + num_cats
        dfwrapper = self.transformers.DataFrameWrapper(ct, columns=columns)
        X = dfwrapper.fit_transform(X)
        self.X_transformers['imputation'] = dfwrapper
        print(f'Imputation taken {time.time() - start}s')
        return X

    def _categorical_encoding(self, X):
        start = time.time()
        logger.info('Categorical encoding...')
        vars = self.get_categorical_columns()
        mle = self.transformers.MultiLabelEncoder(vars)
        X = mle.fit_transform(X)
        self.X_transformers['label_encoder'] = mle
        print(f'Categorical encoding taken {time.time() - start}s')
        return X

    def _discretization(self, X):
        start = time.time()
        logger.info('Data discretization...')
        vars = self.get_continuous_columns()
        mkbd = self.transformers.MultiKBinsDiscretizer(vars)
        X = mkbd.fit_transform(X)
        self.__append_categorical_cols([(new_name, bins + 1) for name, new_name, bins in mkbd.new_columns])
        self.X_transformers['discreter'] = mkbd
        print(f'Discretization taken {time.time() - start}s')
        return X

    def _var_len_encoder(self, X, var_len_categorical_columns):
        start = time.time()
        logger.info('Encoder var length feature...')
        transformer = self.transformers.MultiVarLenFeatureEncoder(var_len_categorical_columns)
        X = transformer.fit_transform(X)

        # update var_len_categorical_columns
        for c in self.var_len_categorical_columns:
            c.max_elements_length = transformer.max_length_[c.name]

        self.X_transformers['var_len_encoder'] = transformer
        print(f'Encoder taken {time.time() - start}s')
        return X

    def _apply_gbm_features(self, X, y):
        start = time.time()
        logger.info('Extracting GBM features...')
        cont_vars = self.get_continuous_columns()
        cat_vars = self.get_categorical_columns()
        gbmencoder = self.transformers.LgbmLeavesEncoder(cat_vars, cont_vars, self.task_, **self.config.gbm_params)
        X = gbmencoder.fit_transform(X, y)
        self.X_transformers['gbm_features'] = gbmencoder
        if self.config.gbm_feature_type == consts.GBM_FEATURE_TYPE_EMB:
            self.__append_categorical_cols(self._gbm_features_to_categorical_cols(X, gbmencoder))
        else:
            self.__append_continuous_cols(self._gbm_features_to_continuous_cols(X, gbmencoder),
                                          consts.INPUT_PREFIX_NUM + 'gbm_leaves')
        print(f'Extracting gbm features taken {time.time() - start}s')
        return X

    def _gbm_features_to_categorical_cols(self, X, gbmencoder):
        return [(name, X[name].max() + 1) for name in gbmencoder.new_columns]

    def _gbm_features_to_continuous_cols(self, X, gbmencoder):
        # return [name for name in gbmencoder.new_columns]
        return gbmencoder.new_columns

    def __append_var_len_categorical_col(self, name, voc_size, sep, pooling_strategy):
        logger.debug(f'Var len categorical variables {name} appended.')

        if self.config.fixed_embedding_dim:
            embedding_output_dim = self.config.embeddings_output_dim if self.config.embeddings_output_dim > 0 else consts.EMBEDDING_OUT_DIM_DEFAULT
        else:
            embedding_output_dim = 0

        if self.var_len_categorical_columns is None:
            self.var_len_categorical_columns = []

        vc = \
            VarLenCategoricalColumn(name,
                                    voc_size,
                                    embedding_output_dim if embedding_output_dim > 0 else min(
                                        4 * int(pow(voc_size, 0.25)), 20),
                                    sep=sep,
                                    pooling_strategy=pooling_strategy)

        self.var_len_categorical_columns.append(vc)

    def __append_categorical_cols(self, cols):
        logger.debug(f'{len(cols)} categorical variables appended.')

        if self.config.fixed_embedding_dim:
            embedding_output_dim = self.config.embeddings_output_dim if self.config.embeddings_output_dim > 0 else consts.EMBEDDING_OUT_DIM_DEFAULT
        else:
            embedding_output_dim = 0
            #

        if self.categorical_columns is None:
            self.categorical_columns = []

        if cols is not None and len(cols) > 0:
            self.categorical_columns = self.categorical_columns + \
                                       [CategoricalColumn(name,
                                                          voc_size,
                                                          embedding_output_dim
                                                          if embedding_output_dim > 0
                                                          else min(4 * int(pow(voc_size, 0.25)), 20))
                                        for name, voc_size in cols]

    def __append_continuous_cols(self, cols, input_name):
        if self.continuous_columns is None:
            self.continuous_columns = []
        if cols is not None and len(cols) > 0:
            self.continuous_columns = self.continuous_columns + [ContinuousColumn(name=input_name,
                                                                                  column_names=[c for c in cols])]

    def get_categorical_columns(self):
        return [c.name for c in self.categorical_columns]

    def get_var_len_categorical_columns(self):
        if self.var_len_categorical_columns is not None:
            return [c.name for c in self.var_len_categorical_columns]
        else:
            return []

    def get_continuous_columns(self):
        cont_vars = []
        for c in self.continuous_columns:
            cont_vars = cont_vars + c.column_names
        return cont_vars

    def _prepare_cache_dir(self, cache_home, clear_cache=False):
        if cache_home is None:
            cache_home = 'cache'
        if cache_home[-1] == '/':
            cache_home = cache_home[:-1]

        cache_home = os.path.expanduser(f'{cache_home}')
        if not fs.exists(cache_home):
            fs.makedirs(cache_home, exist_ok=True)
        else:
            if clear_cache:
                fs.rm(cache_home, recursive=True)
                fs.mkdirs(cache_home, exist_ok=True)
        cache_dir = f'{cache_home}/{self.signature}'
        if not fs.exists(cache_dir):
            fs.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    def get_transformed_X_y_from_cache(self, sign):
        file_x_y = f'{self.cache_dir}/X_y_{sign}.pkl.gz'
        X_t, y_t = None, None
        if fs.exists(file_x_y):
            try:
                with fs.open(file_x_y, mode='rb') as f:
                    df = pd.read_pickle(f, compression='gzip')
                y_t = df.pop('saved__y__')
                X_t = df
            except Exception as e:
                logger.error(e)
                fs.rm(file_x_y)
        return X_t, y_t

    def save_transformed_X_y_to_cache(self, sign, X, y):
        filepath = f'{self.cache_dir}/X_y_{sign}.pkl.gz'
        try:
            # x_t = X.copy(deep=True)
            X.insert(0, 'saved__y__', y)
            with fs.open(filepath, mode='wb') as f:
                X.to_pickle(f, compression='gzip')
            return True
        except Exception as e:
            logger.error(e)
            if fs.exists(filepath):
                fs.rm(filepath)
        return False

    def load_transformers_from_cache(self):
        transformer_path = f'{self.cache_dir}/transformers.pkl'
        if fs.exists(transformer_path):
            try:
                with fs.open(transformer_path, 'rb') as input:
                    preprocessor = pickle.load(input)
                    self.__dict__.update(preprocessor.__dict__)
                    return True
            except Exception as e:
                logger.error(e)
                fs.rm(transformer_path)
        return False

    def save_transformers_to_cache(self):
        transformer_path = f'{self.cache_dir}/transformers.pkl'
        with fs.open(transformer_path, 'wb') as output:
            pickle.dump(self, output, protocol=4)

    def clear_cache(self):
        fs.rm(self.cache_dir, recursive=True)
        fs.makedirs(self.cache_dir, exist_ok=True)


class DefaultDaskPreprocessor(DefaultPreprocessor):
    from hypernets.tabular import dask_ex as dex_

    def __init__(self, config: ModelConfig, compute_to_local=False, cache_home=None, use_cache=False):
        # disable cache
        super(DefaultDaskPreprocessor, self).__init__(config, cache_home=None, use_cache=False)

        if compute_to_local:
            self._wrap_with_compute()

        self.compute_to_local = compute_to_local

    @property
    def transformers(self):
        return self.dex_

    def _wrap_with_compute(self):
        fns = ['fit_transform', 'transform', 'transform_X', 'transform_y', 'inverse_transform_y']
        for fn_name in fns:
            fn_name_original = f'_wrapped_{fn_name}'
            assert not hasattr(self, fn_name_original)
            fn = getattr(self, fn_name)
            assert callable(fn)
            setattr(self, fn_name_original, fn)
            setattr(self, fn_name, partial(self.dex_.call_and_compute, fn, False))

    def _validate_fit_transform(self, X, y):
        assert self.dex_.is_dask_dataframe(X)
        assert self.dex_.is_dask_object(y)

    def _copy(self, obj):
        if self.dex_.is_dask_object(obj):
            return obj.copy()
        else:
            return super()._copy(obj)

    def _get_shape(self, obj):
        if self.dex_.is_dask_object(obj):
            return self.dex_.compute(obj.shape)[0]
        else:
            return super()._get_shape(obj)

    def _nunique(self, y):
        if self.dex_.is_dask_object(y):
            n = y.nunique().compute()
        else:
            n = super()._nunique(y)
        return n

    def _prepare_X(self, X):
        if len(set(X.columns)) != len(list(X.columns)):
            cols = [item for item, count in collections.Counter(X.columns).items() if count > 1]
            raise ValueError(f'Columns with duplicate names in X: {cols}')
        if X.columns.dtype != 'object':
            X.columns = ['x_' + str(c) for c in X.columns]
            logger.warning(f"Column index of X has been converted: {X.columns}")

        return X

    def fit_transform_y(self, y):
        y = super().fit_transform_y(y)

        if self.dex_.is_dask_object(self.labels_):
            self.labels_ = self.labels_.compute()

        return y

    def _gbm_features_to_categorical_cols(self, X, gbmencoder):
        if self.dex_.is_dask_dataframe(X):
            cat_nums = X[gbmencoder.new_columns].max().compute()
            cols = [(name, cats + 1) for name, cats in cat_nums.to_dict().items()]
            return cols
        else:
            return super()._gbm_features_to_categorical_cols(X, gbmencoder)
