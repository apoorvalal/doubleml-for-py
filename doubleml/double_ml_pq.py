import numpy as np
from scipy.optimize import root_scalar
from sklearn.base import clone
from sklearn.utils import check_X_y
from sklearn.model_selection import StratifiedKFold, train_test_split

from .double_ml import DoubleML
from .double_ml_score_mixins import NonLinearScoreMixin
from ._utils import _dml_cv_predict, _trimm, _predict_zero_one_propensity, _check_contains_iv, \
    _check_zero_one_treatment, _check_quantile, _check_treatment, _check_trimming, _check_score, _get_bracket_guess
from .double_ml_data import DoubleMLData
from ._utils_resampling import DoubleMLResampling


class DoubleMLPQ(NonLinearScoreMixin, DoubleML):
    """Double machine learning for potential quantiles

    Parameters
    ----------
    obj_dml_data : :class:`DoubleMLData` object
        The :class:`DoubleMLData` object providing the data and specifying the variables for the causal model.

    ml_g : classifier implementing ``fit()`` and ``predict()``
        A machine learner implementing ``fit()`` and ``predict_proba()`` methods (e.g.
        :py:class:`sklearn.ensemble.RandomForestClassifier`) for the nuisance function
         :math:`g_0(X) = E[Y <= \theta | X, D=d]` .

    ml_m : classifier implementing ``fit()`` and ``predict_proba()``
        A machine learner implementing ``fit()`` and ``predict_proba()`` methods (e.g.
        :py:class:`sklearn.ensemble.RandomForestClassifier`) for the nuisance function :math:`m_0(X) = E[D=d|X]`.

    treatment : int
        Binary treatment indicator. Has to be either ``0`` or ``1``. Determines the potential outcome to be considered.
        Default is ``1``.

    quantile : float
        Quantile of the potential outcome. Has to be between ``0`` and ``1``.
        Default is ``0.5``.

    n_folds : int
        Number of folds.
        Default is ``5``.

    n_rep : int
        Number of repetitons for the sample splitting.
        Default is ``1``.

    score : str
        A str (``'PQ'`` is the only choice) specifying the score function
        for potential quantiles.
        Default is ``'PQ'``.

    dml_procedure : str
        A str (``'dml1'`` or ``'dml2'``) specifying the double machine learning algorithm.
        Default is ``'dml2'``.

    trimming_rule : str
        A str (``'truncate'`` is the only choice) specifying the trimming approach.
        Default is ``'truncate'``.

    trimming_threshold : float
        The threshold used for trimming.
        Default is ``1e-12``.

    h : float or None
        The bandwidth to be used for the kernel density estimation of the score derivative.
        If ``None`` the bandwidth will be set to ``np.power(n_obs, -0.2)``, where ``n_obs`` is
        the number of observations in the sample.
        Default is ``1e-12``.

    normalize : bool
        Indicates whether to normalize weights in the estimation of the score derivative.
        Default is ``True``.

    draw_sample_splitting : bool
        Indicates whether the sample splitting should be drawn during initialization of the object.
        Default is ``True``.

    apply_cross_fitting : bool
        Indicates whether cross-fitting should be applied(``True`` is the only choice).
        Default is ``True``.

    Examples
    --------
    >>> import numpy as np
    >>> import doubleml as dml
    >>> from doubleml.datasets import make_irm_data
    >>> from sklearn.ensemble import RandomForestClassifier
    >>> np.random.seed(3141)
    >>> ml_g = RandomForestRegressor(n_estimators=100, max_features=20, max_depth=5, min_samples_leaf=2)
    >>> ml_m = RandomForestClassifier(n_estimators=100, max_features=20, max_depth=5, min_samples_leaf=2)
    >>> data = make_irm_data(theta=0.5, n_obs=500, dim_x=20, return_type='DataFrame')
    >>> obj_dml_data = dml.DoubleMLData(data, 'y', 'd')
    >>> dml_pq_obj = dml.DoubleMLPQ(obj_dml_data, ml_g, ml_m, treatment=1, quantile=0.5)
    >>> dml_pq_obj.fit().summary
           coef   std err         t     P>|t|     2.5 %    97.5 %
    d  0.566897  0.121525  4.664862  0.000003  0.328712  0.805081
    """

    def __init__(self,
                 obj_dml_data,
                 ml_g,
                 ml_m,
                 treatment,
                 quantile=0.5,
                 n_folds=5,
                 n_rep=1,
                 score='PQ',
                 dml_procedure='dml2',
                 trimming_rule='truncate',
                 trimming_threshold=1e-12,
                 h=None,
                 normalize=True,
                 draw_sample_splitting=True,
                 apply_cross_fitting=True):
        super().__init__(obj_dml_data,
                         n_folds,
                         n_rep,
                         score,
                         dml_procedure,
                         draw_sample_splitting,
                         apply_cross_fitting)

        self._quantile = quantile
        self._treatment = treatment
        self._h = h
        if self.h is None:
            self._h = np.power(self._dml_data.n_obs, -0.2)
        self._normalize = normalize

        if self._is_cluster_data:
            raise NotImplementedError('Estimation with clustering not implemented.')
        self._check_data(self._dml_data)

        valid_score = ['PQ']
        _check_score(self.score, valid_score)
        _check_quantile(self.quantile)
        _check_treatment(self.treatment)

        self._check_bandwidth(self.h)
        if not isinstance(self.normalize, bool):
            raise TypeError('Normalization indicator has to be boolean. ' +
                            f'Object of type {str(type(self.normalize))} passed.')

        # initialize starting values and bounds
        self._coef_bounds = (self._dml_data.y.min(), self._dml_data.y.max())
        self._coef_start_val = np.quantile(self._dml_data.y, self.quantile)

        # initialize and check trimming
        self._trimming_rule = trimming_rule
        self._trimming_threshold = trimming_threshold
        _check_trimming(self._trimming_rule, self._trimming_threshold)

        _ = self._check_learner(ml_g, 'ml_g', regressor=False, classifier=True)
        _ = self._check_learner(ml_m, 'ml_m', regressor=False, classifier=True)
        self._learner = {'ml_g': clone(ml_g), 'ml_m': clone(ml_m)}
        self._predict_method = {'ml_g': 'predict_proba', 'ml_m': 'predict_proba'}

        self._initialize_ml_nuisance_params()

        if draw_sample_splitting:
            obj_dml_resampling = DoubleMLResampling(n_folds=self.n_folds,
                                                    n_rep=self.n_rep,
                                                    n_obs=self._dml_data.n_obs,
                                                    apply_cross_fitting=self.apply_cross_fitting,
                                                    stratify=self._dml_data.d)
            self._smpls = obj_dml_resampling.split_samples()

    @property
    def quantile(self):
        """
        Quantile for potential outcome.
        """
        return self._quantile

    @property
    def treatment(self):
        """
        Treatment indicator for potential outcome.
        """
        return self._treatment

    @property
    def h(self):
        """
        The bandwidth the kernel density estimation of the derivative.
        """
        return self._h

    @property
    def normalize(self):
        """
        Indicates of the weights in the derivative estimation should be normalized.
        """
        return self._normalize

    @property
    def trimming_rule(self):
        """
        Specifies the used trimming rule.
        """
        return self._trimming_rule

    @property
    def trimming_threshold(self):
        """
        Specifies the used trimming threshold.
        """
        return self._trimming_threshold

    @property
    def _score_element_names(self):
        return ['ind_d', 'g', 'm', 'y']

    def _compute_ipw_score(self, theta, d, y, prop):
        score = (d == self.treatment) / prop * (y <= theta) - self.quantile
        return score

    def _compute_score(self, psi_elements, coef, inds=None):
        ind_d = psi_elements['ind_d']
        g = psi_elements['g']
        m = psi_elements['m']
        y = psi_elements['y']

        if inds is not None:
            ind_d = psi_elements['ind_d'][inds]
            g = psi_elements['g'][inds]
            m = psi_elements['m'][inds]
            y = psi_elements['y'][inds]

        score = ind_d * ((y <= coef) - g) / m + g - self.quantile
        return score

    def _compute_score_deriv(self, psi_elements, coef, inds=None):
        ind_d = psi_elements['ind_d']
        m = psi_elements['m']
        y = psi_elements['y']

        if inds is not None:
            ind_d = psi_elements['ind_d'][inds]
            m = psi_elements['m'][inds]
            y = psi_elements['y'][inds]

        score_weights = ind_d / m
        normalization = score_weights.mean()
        if self._normalize:
            score_weights /= normalization

        u = (y - coef).reshape(-1, 1) / self._h
        kernel_est = np.exp(-1. * np.power(u, 2) / 2) / np.sqrt(2 * np.pi)
        deriv = np.multiply(score_weights, kernel_est.reshape(-1, )) / self._h

        return deriv

    def _initialize_ml_nuisance_params(self):
        self._params = {learner: {key: [None] * self.n_rep for key in self._dml_data.d_cols}
                        for learner in ['ml_g', 'ml_m']}

    def _nuisance_est(self, smpls, n_jobs_cv, return_models=False):
        x, y = check_X_y(self._dml_data.x, self._dml_data.y,
                         force_all_finite=False)
        x, d = check_X_y(x, self._dml_data.d,
                         force_all_finite=False)

        # initialize nuisance predictions
        g_hat = np.full(shape=self._dml_data.n_obs, fill_value=np.nan)
        m_hat = np.full(shape=self._dml_data.n_obs, fill_value=np.nan)

        # caculate nuisance functions over different folds
        for i_fold in range(self.n_folds):
            train_inds = smpls[i_fold][0]
            test_inds = smpls[i_fold][1]

            # start nested crossfitting
            train_inds_1, train_inds_2 = train_test_split(train_inds, test_size=0.5,
                                                          random_state=42, stratify=d[train_inds])
            smpls_prelim = [(train, test) for train, test in
                            StratifiedKFold(n_splits=self.n_folds).split(X=train_inds_1, y=d[train_inds_1])]

            d_train_1 = d[train_inds_1]
            y_train_1 = y[train_inds_1]
            x_train_1 = x[train_inds_1, :]

            m_hat_prelim = _dml_cv_predict(self._learner['ml_m'], x_train_1, d_train_1,
                                           method='predict_proba', smpls=smpls_prelim)['preds']

            m_hat_prelim = _trimm(m_hat_prelim, self.trimming_rule, self.trimming_threshold)
            if self.treatment == 0:
                m_hat_prelim = 1 - m_hat_prelim

            # preliminary ipw estimate
            def ipw_score(theta):
                res = np.mean(self._compute_ipw_score(theta, d_train_1, y_train_1, m_hat_prelim))
                return res

            _, bracket_guess = _get_bracket_guess(ipw_score, self._coef_start_val, self._coef_bounds)
            root_res = root_scalar(ipw_score,
                                   bracket=bracket_guess,
                                   method='brentq')
            ipw_est = root_res.root

            # readjust start value for minimization
            self._coef_start_val = ipw_est

            # use the preliminary estimates to fit the nuisance parameters on train_2
            d_train_2 = d[train_inds_2]
            y_train_2 = y[train_inds_2]
            x_train_2 = x[train_inds_2, :]

            dx_treat_train_2 = x_train_2[d_train_2 == self.treatment, :]
            y_treat_train_2 = y_train_2[d_train_2 == self.treatment]
            self._learner['ml_g'].fit(dx_treat_train_2, y_treat_train_2 <= ipw_est)

            # predict nuisance values on the test data
            g_hat[test_inds] = _predict_zero_one_propensity(self._learner['ml_g'], x[test_inds, :])

            # refit the propensity score on the whole training set
            self._learner['ml_m'].fit(x[train_inds, :], d[train_inds])
            m_hat[test_inds] = _predict_zero_one_propensity(self._learner['ml_m'], x[test_inds, :])

        if self.treatment == 0:
            m_hat = 1 - m_hat
        # clip propensities
        m_hat = _trimm(m_hat, self.trimming_rule, self.trimming_threshold)

        psi_elements = {'ind_d': d == self.treatment, 'g': g_hat,
                        'm': m_hat, 'y': y}
        preds = {'ml_g': g_hat, 'ml_m': m_hat}
        return psi_elements, preds

    def _nuisance_tuning(self, smpls, param_grids, scoring_methods, n_folds_tune, n_jobs_cv,
                         search_mode, n_iter_randomized_search):
        raise NotImplementedError('Nuisance tuning not implemented for potential quantiles.')

    def _check_data(self, obj_dml_data):
        if not isinstance(obj_dml_data, DoubleMLData):
            raise TypeError('The data must be of DoubleMLData type. '
                            f'{str(obj_dml_data)} of type {str(type(obj_dml_data))} was passed.')
        _check_contains_iv(obj_dml_data)
        _check_zero_one_treatment(self)
        return

    def _check_bandwidth(self, bandwidth):
        if not isinstance(bandwidth, float):
            raise TypeError('Bandwidth has to be a float. ' +
                            f'Object of type {str(type(bandwidth))} passed.')

        if bandwidth <= 0:
            raise ValueError('Bandwidth has be positive. ' +
                             f'Bandwidth {str(bandwidth)} passed.')
        return
