import pyhdfe
import re

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt

from typing import Any, Union, Dict, Optional, List, Tuple
from scipy.stats import norm
from formulaic import model_matrix

from pyfixest.feols import Feols
from pyfixest.FormulaParser import FixestFormulaParser, _flatten_list
from pyfixest.ssc_utils import ssc


class DepvarIsNotNumericError(Exception):
    pass


class Fixest:

    def __init__(self, data: pd.DataFrame) -> None:
        '''
        A class for fixed effects regression modeling.
        Args:
            data: The input pd.DataFrame for the object.
        Returns:
            None
        '''

        self.data = data
        self.model_res = dict()

    def _clean_fe(self, data, fval):

        fval_list = fval.split("+")

        # find interacted fixed effects via "^"
        interacted_fes = [x for x in fval_list if '^' in x]
             
        varying_slopes = [x for x in fval_list if '/' in x]

        for x in interacted_fes:
            vars = x.split("^")
            data[x] = data[vars].fillna(method='ffill', axis=1).apply(lambda x: '^'.join(
                row.dropna().astype(str)), axis=1)

        fe = data[fval_list]
        # all fes to factors / categories

        if varying_slopes != []: 
          
            for x in varying_slopes: 
                mm_vs = model_matrix("-1 + " + x, data)
            
            fe = pd.concat([fe, mm_vs], axis = 1)
        
        fe_na = fe.isna().any(axis=1)
        fe = fe.apply(lambda x: pd.factorize(x)[0])
        fe = fe.to_numpy()

        return fe, fe_na

    def _demean_model(self, data: pd.DataFrame, fval: str, ivars: List[str], drop_ref: str) -> None:
        '''
        Demean all regressions for a specification of fixed effects.

        Args:
            data: The input pd.DataFrame for the object. Either self.data or a subset thereof (for split sample estimation).
            fval: A specification of fixed effects. A string indicating the fixed effects to be demeaned,
                such as "X4" or "X3 + X2".
            ivars: A list of strings indicating the interacted variables via i().
            drop_ref: A string indicating the reference category for the interacted variables. The reference
                      category is dropped from the regression.

        Returns:
            None
        '''

        YXZ_dict = dict()
        na_dict = dict()
        var_dict = dict()

        if fval != "0":
            fe, fe_na = self._clean_fe(data, fval)
            fe_na = list(fe_na[fe_na == True])
        else:
            fe = None
            fe_na = None

        dict2fe = self.fml_dict2.get(fval)
        if self.is_iv:
            dict2fe_iv = self.fml_dict2_iv.get(fval)

        # create lookup table with NA index key
        # for each regression, check if lookup table already
        # populated with "demeaned" data for some (or all)
        # variables of the model. if demeaned variable for
        # NA key already exists -> use it. else rerun demeaning
        # algorithm

        lookup_demeaned_data = dict()

        # loop over both dict2fe and dict2fe_iv (if the latter is not None)
        for depvar in dict2fe.keys():

            # [(0, 'X1+X2'), (1, ['X1+X3'])]
            for _, covar in enumerate(dict2fe.get(depvar)):

                covar2 = covar
                depvar2 = depvar

                fml = depvar2 + " ~ " + covar2

                if self.is_iv:
                    instruments2 = dict2fe_iv.get(depvar)[0]
                    endogvar_list = list(set(covar2.split("+")) - set(instruments2.split("+")))#[0]
                    instrument_list = list(set(instruments2.split("+")) - set(covar2.split("+")))#[0]

                    fml2 = "+".join(instrument_list) + "+" + fml

                else:
                    fml2 = fml

                lhs, rhs = model_matrix(fml2, data)

                untransformed_depvar = _find_untransformed_depvar(depvar2)

                Y = lhs[[depvar]]
                X = rhs
                if self.is_iv:
                    I = lhs[instrument_list]

                # get NA index before converting Y to numpy array
                na_index = list(set(data.index) - set(Y.index))

                # drop variables before collecting variable names
                if self.ivars is not None:
                    if drop_ref is not None:
                        X = X.drop(drop_ref, axis=1)

                y_names = list(Y.columns)
                x_names = list(X.columns)
                yxz_names = list(y_names) + list(x_names)
                if self.is_iv:
                    iv_names = list(I.columns)
                    x_names_copy = x_names.copy()
                    x_names_copy = [x for x in x_names_copy if x not in endogvar_list]
                    z_names = x_names_copy + instrument_list
                    cols = yxz_names + iv_names
                else:
                    iv_names = None
                    z_names = None
                    cols = yxz_names

                if self.ivars is not None:
                    self.icovars = [s for s in x_names if s.startswith(
                        ivars[0]) and s.endswith(ivars[1])]
                else:
                    self.icovars = None


                Y = Y.to_numpy()
                X = X.to_numpy()
                if self.is_iv:
                    I = I.to_numpy()

                if Y.shape[1] > 1:
                    raise ValueError(
                        "Dependent variable must be a single column. Please make sure that the dependent variable" + depvar2 + "is of a numeric type (int or float).")

                # variant 1: if there are fixed effects to be projected out
                if fe is not None:
                    na_index = (na_index + fe_na)
                    fe2 = np.delete(fe, na_index, axis=0)
                    # drop intercept
                    intercept_index = x_names.index("Intercept")
                    X = np.delete(X, intercept_index, axis = 1)
                    x_names.remove("Intercept")
                    yxz_names.remove("Intercept")
                    if self.is_iv:
                        z_names.remove("Intercept")
                        cols.remove("Intercept")

                    # check if variables have already been demeaned
                    Y = np.delete(Y, fe_na, axis=0)
                    X = np.delete(X, fe_na, axis=0)
                    if self.is_iv:
                        I = np.delete(I, fe_na, axis=0)

                    if self.is_iv:
                        YXZ = np.concatenate([Y, X, I], axis = 1)
                    else:
                        YXZ = np.concatenate([Y, X], axis=1)

                    na_index_str = ','.join(str(x) for x in na_index)

                    # check if looked dict has data for na_index
                    if lookup_demeaned_data.get(na_index_str) is not None:
                        # get data out of lookup table: list of [algo, data]
                        algorithm, YXZ_demeaned_old = lookup_demeaned_data.get(
                            na_index_str)

                        # get not yet demeaned covariates
                        var_diff_names = list(
                            set(yxz_names) - set(YXZ_demeaned_old.columns))[0]
                        var_diff_index = list(yxz_names).index(var_diff_names)
                        var_diff = YXZ[:, var_diff_index]
                        if var_diff.ndim == 1:
                            var_diff = var_diff.reshape(len(var_diff), 1)

                        YXZ_demean_new = algorithm.residualize(var_diff)
                        YXZ_demeaned = np.concatenate(
                            [YXZ_demeaned_old, YXZ_demean_new], axis=1)
                        YXZ_demeaned = pd.DataFrame(YXZ_demeaned)

                        YXZ_demeaned.columns = list(
                            YXZ_demeaned_old.columns) + [var_diff_names]

                    else:
                        # not data demeaned yet for NA combination
                        algorithm = pyhdfe.create(
                            ids=fe2,
                            residualize_method='map',
                            drop_singletons=self.drop_singletons,
                        )

                        if self.drop_singletons == True and algorithm.singletons != 0 and algorithm.singletons is not None:
                            print(algorithm.singletons, "columns are dropped due to singleton fixed effects.")
                            dropped_singleton_indices = (
                                np.where(algorithm._singleton_indices))[0].tolist()
                            na_index += dropped_singleton_indices

                        YXZ_demeaned = algorithm.residualize(YXZ)
                        YXZ_demeaned = pd.DataFrame(YXZ_demeaned)

                        YXZ_demeaned.columns = cols

                    lookup_demeaned_data[na_index_str] = [
                        algorithm, YXZ_demeaned]

                else:
                    # if no fixed effects
                    if self.is_iv:
                        YXZ = np.concatenate([Y, X, I], axis=1)
                    else:
                        YXZ = np.concatenate([Y, X], axis=1)

                    YXZ_demeaned = pd.DataFrame(YXZ)

                    YXZ_demeaned.columns = cols

                YXZ_dict[fml] = YXZ_demeaned
                na_dict[fml] = na_index
                var_dict[fml] = dict({
                    'y_names': y_names,
                    'x_names': x_names,
                    'iv_names': iv_names,
                    'z_names': z_names
                })


        return YXZ_dict, na_dict, var_dict

    def _demean_all_models(self, fixef_keys, ivars, drop_ref, estimate_full_model, estimate_split_model):

        '''
        demean multiple models. essentially, the function loops
        over all split var and fixed effects variables and demeans the
        specified dependend variables and covariates
        Args:
            fixef_keys: fixed effect variables
            ivars: interaction variables
            drop_ref: drop reference category
            estimate_full_model: boolean, whether to estimate the full model
            estimate_split_model: boolean, whether to estimate the split model
        '''

        if estimate_full_model:
            for _, fval in enumerate(fixef_keys):
                self.demeaned_data_dict[fval] = []
                self.dropped_data_dict[fval] = []
                self.yxz_name_dict[fval] = []
                data = self.data
                demeaned_data, dropped_data, yxz_name_dict= self._demean_model(
                    data, fval, ivars, drop_ref)
                self.demeaned_data_dict[fval].append(demeaned_data)
                self.dropped_data_dict[fval].append(dropped_data)
                self.yxz_name_dict[fval].append(yxz_name_dict)

        if estimate_split_model:
            for _, fval in enumerate(fixef_keys):
                self.demeaned_data_dict[fval] = []
                self.dropped_data_dict[fval] = []
                self.yxz_name_dict[fval] = []
                for x in self.split_categories:
                    sub_data = self.data[x == self.splitvar]
                    demeaned_data, dropped_data, yxz_name_dict = self._demean_model(
                        sub_data, fval, ivars, drop_ref)
                    self.demeaned_data_dict[fval].append(demeaned_data)
                    self.dropped_data_dict[fval].append(dropped_data)
                    self.yxz_name_dict[fval].append(yxz_name_dict)

    def _estimate_all_models(self, vcov):

        # estimate models based on demeaned model matrix and dependent variables
        for _, fval in enumerate(self.fml_dict.keys()):
            model_splits = self.demeaned_data_dict[fval]
            for x, _ in enumerate(model_splits):
                model_frames = model_splits[x]
                for _, fml in enumerate(model_frames):

                    # get the (demeaned) model frame. key is fml without fixed effects
                    model_frame = model_frames[fml]

                    # update formula with fixed effect. fval is "0" for no fixed effect
                    if fval == "0":
                        fml2 = fml
                    else:
                        fml2 = fml + "|" + fval

                    # formula log: add information on sample split
                    if self.splitvar is not None:
                        split_log = str(self.split_categories[x])
                        full_fml = fml2 + "| split =" + split_log
                    else:
                        split_log = None
                        full_fml = fml2

                    name_dict = self.yxz_name_dict[fval][0][fml]
                    depvar_name = name_dict["y_names"]
                    xvar_names = name_dict["x_names"]
                    if name_dict["z_names"] is None:
                        zvar_names = name_dict["x_names"]
                    else:
                        zvar_names = name_dict["z_names"]

                    Y = model_frame[depvar_name]
                    X = model_frame[xvar_names]
                    Z = model_frame[zvar_names]

                    colnames = X.columns
                    zcolnames = Z.columns

                    Y = Y.to_numpy()
                    X = X.to_numpy()
                    Z = Z.to_numpy()

                    N = X.shape[0]
                    k = X.shape[1]

                    # check for multicollinearity
                    _multicollinearity_checks(X, Z, self.ivars, fml2)

                    FEOLS = Feols(Y, X, Z)
                    FEOLS.is_iv = self.is_iv
                    FEOLS.fml = fml2
                    FEOLS.ssc_dict = self.ssc_dict
                    if self.is_iv:
                        FEOLS.get_fit(estimator = "2sls")
                    else:
                        FEOLS.get_fit(estimator = "ols")
                    FEOLS.na_index = self.dropped_data_dict[fval][x][fml]
                    FEOLS.data = self.data.iloc[~self.data.index.isin(
                        FEOLS.na_index), :]
                    FEOLS.N = N
                    FEOLS.k = k
                    if fval != "0":
                        FEOLS.has_fixef = True
                        FEOLS.fixef = fval
                    else:
                        FEOLS.has_fixef = False

                    vcov_type = _get_vcov_type(vcov, fval)

                    FEOLS.vcov_log = vcov_type
                    FEOLS.split_log = x
                    FEOLS.get_vcov(vcov=vcov_type)
                    FEOLS.get_inference()
                    FEOLS.coefnames = colnames
                    if self.icovars is not None:
                        FEOLS.icovars = self.icovars
                    self.model_res[full_fml] = FEOLS



    def feols(self, fml: str, vcov: Union[None, str, Dict[str, str]] = None, ssc=ssc(), fixef_rm: str = "none") -> None:
        '''
        Method for fixed effects regression modeling using the PyHDFE package for projecting out fixed effects.
        Args:
            fml (str): A three-sided formula string using fixest formula syntax. Supported syntax includes:
                The syntax is as follows: "Y ~ X1 + X2 | FE1 + FE2 | X1 ~ Z1" where:

                Y: Dependent variable
                X1, X2: Independent variables
                FE1, FE2: Fixed effects
                Z1, Z2: Instruments
                |: Separates left-hand side, fixed effects, and instruments

                If no fixed effects and instruments are specified, the formula can be simplified to "Y ~ X1 + X2".
                If no instruments are specified, the formula can be simplified to "Y ~ X1 + X2 | FE1 + FE2".
                If no fixed effects are specified but instruments are specified, the formula can be simplified to "Y ~ X1 + X2 | X1 ~ Z1".

                Supported multiple estimation syntax includes:

                Stepwise regressions (sw, sw0)
                Cumulative stepwise regression (csw, csw0)
                Multiple dependent variables (Y1 + Y2 ~ X)

                Other special syntax includes:
                i() for interaction of a categorical and non-categorical variable (e.g. "i(X1,X2)" for interaction between X1 and X2).
                Using i() is required to use with some custom methods, e.g. iplot().
                ^ for interacted fixed effects (e.g. "fe1^fe2" for interaction between fe1 and fe2)

                All other parts of the formula must be compatible with formula parsing via the formulaic module.
                You can use formulaic functionaloty such as "C", "I", ":",, "*", "np.log", "np.power", etc.

            vcov (Union(str, dict)): A string or dictionary specifying the type of variance-covariance matrix to use for inference.
                If a string, it can be one of "iid", "hetero", "HC1", "HC2", "HC3".
                If a dictionary, it should have the format dict("CRV1":"clustervar") for CRV1 inference or dict(CRV3":"clustervar") for CRV3 inference.
            fixef_rm: A string specifiny whether singleton fixed effects should be dropped. Options are "none" (default) and "singleton". If "singleton", singleton fixed effects are dropped.
        Returns:
            None
        Examples:
            Standard formula:
                fml = 'Y ~ X1 + X2'
                fixest_model = Fixest(data=data).feols(fml, vcov='iid')
            With fixed effects:
                fml = 'Y ~ X1 + X2 | fe1 + fe2'
            With interacted fixed effects:
                fml = 'Y ~ X1 + X2 | fe1^fe2'
            Multiple dependent variables:
                fml = 'Y1 + Y2 ~ X1 + X2'
            Stepwise regressions (sw and sw0):
                fml = 'Y1 + Y2 ~ sw(X1, X2, X3)'
            Cumulative stepwise regressions (csw and csw0):
                fml = 'Y1 + Y2 ~ csw(X1, X2, X3) '
            Combinations:
                fml = 'Y1 + Y2 ~ csw(X1, X2, X3) | sw(X4, X5) + X6'

        Details:
            The method proceeds in the following steps:
            1. Parse the formula using the FixestFormulaParser class.
            2. Create a dictionary of formulas for each dependent variable.
            3. demean all models and store the data
            4. fit all models
        '''

        self.fml = fml.replace(" ", "")
        self.split = None

        # deparse formula, at least partially
        fxst_fml = FixestFormulaParser(fml)

        if fxst_fml.is_iv:
            self.is_iv = True
        else:
            self.is_iv = False

        # add function argument to these methods for IV
        fxst_fml.get_fml_dict()
        fxst_fml.get_var_dict()
        fxst_fml._transform_fml_dict()

        if self.is_iv:
            # create required dicts for first stage IV regressions
            fxst_fml.get_fml_dict(iv = True)
            fxst_fml.get_var_dict(iv = True)
            fxst_fml._transform_fml_dict(iv = True)


        self.fml_dict = fxst_fml.fml_dict
        self.var_dict = fxst_fml.var_dict
        self.fml_dict2 = fxst_fml.fml_dict2

        if self.is_iv:
            self.fml_dict_iv = fxst_fml.fml_dict_iv
            self.var_dict_iv = fxst_fml.var_dict_iv
            self.fml_dict2_iv = fxst_fml.fml_dict2_iv
        else:
            self.fml_dict_iv = self.fml_dict
            self.var_dict_iv = self.var_dict
            self.fml_dict2_iv = self.fml_dict2

        self.ivars = fxst_fml.ivars


        self.ssc_dict = ssc
        self.drop_singletons = _drop_singletons(fixef_rm)

        # get all fixed effects combinations
        fixef_keys = list(self.var_dict.keys())

        ivars, drop_ref = _clean_ivars(self.ivars, self.data)

        # dropped_data_dict and demeaned_data_dict are
        # dictionaries with keys for each fixed effects combination and
        # has values of lists of demeaned dataframes
        # the list is a singelton list unless split sample estimation is used
        # e.g it looks like this (without split estimation):
        # {'fe1': [demeaned_data_df], 'fe1+fe2': [demeaned_data_df]}
        # and like this (with split estimation):
        # {'fe1': [demeaned_data_df1, demeaned_data_df2], 'fe1+fe2': [demeaned_data_df1, demeaned_data_df2]}
        # the lists are sorted in the order of the split variable

        self.dropped_data_dict = dict()
        self.demeaned_data_dict = dict()
        # names of depvar, X, Z matrices
        self.yxz_name_dict = dict()

        estimate_full_model = True
        estimate_split_model = False
        # currently no fsplit allowed
        fsplit = None

        self.splitvar, _, estimate_split_model, estimate_full_model = _prepare_split_estimation(self.split, fsplit, self.data, self.var_dict)

        # demean all models: based on fixed effects x split x missing value combinations
        self._demean_all_models(fixef_keys, ivars, drop_ref, estimate_full_model, estimate_split_model)

        # create self.is_fixef_multi flag
        self._is_multiple_estimation()

        if self.is_fixef_multi and self.is_iv:
            raise ValueError("Multiple Estimations is currently not supported with IV. This is mostly due to insufficient testing and will be possible with the next release of PyFixest.")

        # estimate all regression models based on demeaned data
        self._estimate_all_models(vcov = vcov)

        return self


    def _is_multiple_estimation(self):

        '''
        helper method to check if multiple regression models will be estimated
        '''

        self.is_fixef_multi = False
        if len(self.fml_dict.keys()) > 1:
            self.is_fixef_multi = True
        elif len(self.fml_dict.keys()) == 1:
            first_key = next(iter(self.fml_dict))
            if len(self.fml_dict[first_key]) > 1:
                self.is_fixef_multi = True


    def vcov(self, vcov: Union[str, Dict[str, str]]) -> None:
        '''
        Update regression inference "on the fly".
        By calling vcov() on a "Fixest" object, all inference procedures applied
        to the "Fixest" object are replaced with the variance covariance matrix specified via the method.
        Args:
            vcov: A string or dictionary specifying the type of variance-covariance matrix to use for inference.
                If a string, can be one of "iid", "hetero", "HC1", "HC2", "HC3".
                If a dictionary, it should have the format {"CRV1":"clustervar"} for CRV1 inference
                or {"CRV3":"clustervar"} for CRV3 inference.
        Returns:
            None
        '''

        self.vcov_log = vcov

        for model in list(self.model_res.keys()):

            fxst = self.model_res[model]
            fxst.vcov_log = vcov

            fxst.get_vcov(vcov=vcov)
            fxst.get_inference()

        return self

    def tidy(self, type: Optional[str] = None) -> Union[pd.DataFrame, str]:
        '''
        Returns the results of an estimation using `feols()` as a tidy Pandas DataFrame.
        Args:
            type : str, optional
                The type of output format to use. If set to "markdown", the resulting DataFrame
                will be returned in a markdown format with three decimal places. Default is None.
        Returns:
            pd.DataFrame or str
                A tidy DataFrame with the following columns:
                - fml: the formula used to generate the results
                - coefnames: the names of the coefficients
                - Estimate: the estimated coefficients
                - Std. Error: the standard errors of the estimated coefficients
                - t value: the t-values of the estimated coefficients
                - Pr(>|t|): the p-values of the estimated coefficients
                If `type` is set to "markdown", the resulting DataFrame will be returned as a
                markdown-formatted string with three decimal places.
        '''

        res = []
        for x in list(self.model_res.keys()):

            fxst = self.model_res[x]

            res.append(
                pd.DataFrame(
                    {
                        'fml': x,
                        'coefnames': fxst.coefnames,
                        'Estimate': fxst.beta_hat,
                        'Std. Error': fxst.se,
                        't value': fxst.tstat,
                        'Pr(>|t|)': fxst.pvalue
                    }
                )
            )

        res = pd.concat(res, axis=0).set_index('fml')
        if type == "markdown":
            return res.to_markdown(floatfmt=".3f")
        else:
            return res

    def summary(self) -> None:
        '''
        Prints a summary of the feols() estimation results for each estimated model.
        For each model, the method prints a header indicating the fixed-effects and the
        dependent variable, followed by a table of coefficient estimates with standard
        errors, t-values, and p-values.
        Returns:
            None
        '''

        for x in list(self.model_res.keys()):

            split = x.split("|")
            if len(split) > 1:
                fe = split[1]
            else:
                fe = None
            depvar = split[0].split("~")[0]
            fxst = self.model_res[x]
            df = pd.DataFrame(
                {
                    '': fxst.coefnames,
                    'Estimate': fxst.beta_hat,
                    'Std. Error': fxst.se,
                    't value': fxst.tstat,
                    'Pr(>|t|)': fxst.pvalue
                }
            )

            if fxst.is_iv:
                estimation_method = "IV"
            else:
                estimation_method = "OLS"


            print('###')
            print('')
            print('Model: ', estimation_method)
            print('Dep. var.: ', depvar)
            if fe is not None:
                print('Fixed effects: ', fe)
            # if fxst.split_log is not None:
            #    print('Split. var: ', self.split + ":" + fxst.split_log)
            print('Inference: ', fxst.vcov_log)
            print('Observations: ', fxst.N)
            print('')
            print(df.to_string(index=False))
            print('---')

    def coef(self) -> pd.DataFrame:
        '''
        Obtain the coefficients of the fitted models.
        Returns:
            A pd.DataFrame with coefficient names and Estimates. The key indicates which models the estimated statistic derives from.
        '''

        df = self.tidy()
        return df[['coefnames', 'Estimate']]

    def se(self)-> pd.DataFrame:
        '''
        Obtain the standard errors of the fitted models.

        Returns:
            A pd.DataFrame with coefficient names and standard error estimates. The key indicates which models the estimated statistic derives from.

        '''

        df = self.tidy()
        return df[['coefnames', 'Std. Error']]

    def tstat(self)-> pd.DataFrame:
        '''
        Obtain the t-statistics of the fitted models.

         Returns:
            A pd.DataFrame with coefficient names and estimated t-statistics. The key indicates which models the estimated statistic derives from.

        '''

        df = self.tidy()
        return df[['coefnames', 't value']]

    def pvalue(self) -> pd.DataFrame:
        '''
        Obtain the p-values of the fitted models.

        Returns:
            A pd.DataFrame with coefficient names and p-values. The key indicates which models the estimated statistic derives from.

        '''

        df = self.tidy()
        return df[['coefnames', 'Pr(>|t|)']]

    def iplot(self, alpha: float = 0.05, figsize: tuple = (10, 10), yintercept: Union[int, str, None] = None, xintercept: Union[int, str, None] = None, rotate_xticks: int = 0) -> None:
        '''
        Plot model coefficients with confidence intervals for variable interactions specified via the `i()` syntax.
        Args:
            alpha: float, optional. The significance level for the confidence intervals. Default is 0.05.
            figsize: tuple, optional. The size of the figure. Default is (10, 10).
            yintercept: int or str (for a categorical x axis). The value at which to draw a horizontal line.
            xintercept: int or str (for a categorical y axis). The value at which to draw a vertical line.
        Returns:
            None
        '''

        ivars = self.icovars

        if ivars is None:
            raise ValueError(
                "The estimated models did not have ivars / 'i()' model syntax. In consequence, the '.iplot()' method is not supported.")

        ivars_keys = self.ivars.keys()
        if ivars_keys is not None:
            ref = list(ivars_keys)[0]
        else:
            ref = None

        if "Intercept" in ivars:
            ivars.remove("Intercept")

        df = self.tidy()

        df = df[df.coefnames.isin(ivars)]
        models = df.index.unique()

        _coefplot(
            models=models,
            figsize=figsize,
            alpha=alpha,
            yintercept=yintercept,
            xintercept=xintercept,
            df=df,
            is_iplot=True
        )

    def coefplot(self, alpha: float = 0.05, figsize: tuple = (5, 2), yintercept: int = 0, figtitle: str = None, figtext: str = None, rotate_xticks: int = 0) -> None:
        '''
        Plot estimation results. The plot() method is only defined for single regressions.
        Args:
            alpha (float): the significance level for the confidence intervals. Default is 0.05.
            figsize (tuple): the size of the figure. Default is (5, 2).
            yintercept (float): the value of the y-intercept. Default is 0.
            figtitle (str): the title of the figure. Default is None.
            figtext (str): the text at the bottom of the figure. Default is None.
        Returns:
            None
        '''

        df = self.tidy()
        models = df.index.unique()

        _coefplot(
            models=models,
            figsize=figsize,
            alpha=alpha,
            yintercept=yintercept,
            xintercept=None,
            df=df,
            is_iplot=False,
            rotate_xticks=rotate_xticks
        )

    def wildboottest(self, B, param: Union[str, None] = None, weights_type: str = 'rademacher', impose_null: bool = True, bootstrap_type: str = '11', seed: Union[str, None] = None, adj: bool = True, cluster_adj: bool = True) -> pd.DataFrame:

        '''
        Run a wild cluster bootstrap for all regressions in the Fixest object.

        Args:

            B (int): The number of bootstrap iterations to run
            param (Union[str, None], optional): A string of length one, containing the test parameter of interest. Defaults to None.
            weights_type (str, optional): The type of bootstrap weights. Either 'rademacher', 'mammen', 'webb' or 'normal'.
                                'rademacher' by default. Defaults to 'rademacher'.
            impose_null (bool, optional): Should the null hypothesis be imposed on the bootstrap dgp, or not?
                                Defaults to True.
            bootstrap_type (str, optional):A string of length one. Allows to choose the bootstrap type
                                to be run. Either '11', '31', '13' or '33'. '11' by default. Defaults to '11'.
            seed (Union[str, None], optional): Option to provide a random seed. Defaults to None.

        Returns:
            A pd.DataFrame with bootstrapped t-statistic and p-value. The index indicates which model the estimated statistic derives from.
        '''


        res = []
        for x in list(self.model_res.keys()):

            fxst = self.model_res[x]

            if hasattr(fxst, 'clustervar'):
                cluster = fxst.clustervar
            else:
                cluster = None

            boot_res = fxst.get_wildboottest(B, cluster, param,  weights_type, impose_null, bootstrap_type, seed, adj, cluster_adj)

            pvalue = boot_res["pvalue"]
            tstat = boot_res["statistic"]


            res.append(
                pd.Series(
                    {
                        'fml': x,
                        'param':param,
                        't value': tstat,
                        'Pr(>|t|)': pvalue
                    }
                )
            )

        res = pd.concat(res, axis=1).T.set_index('fml')

        return res


def _coefplot(models: List, df: pd.DataFrame, figsize: Tuple[int, int], alpha: float, yintercept: Optional[int] = None,
              xintercept: Optional[int] = None, is_iplot: bool = False,
              rotate_xticks: float = 0) -> None:
    """
        Plot model coefficients with confidence intervals.
        Args:
            models (list): A list of fitted models.
            figsize (tuple): The size of the figure.
            alpha (float): The significance level for the confidence intervals.
            yintercept (int or None): The value at which to draw a horizontal line on the plot.
            xintercept (int or None): The value at which to draw a vertical line on the plot.
            df (pandas.DataFrame): The dataframe containing the data used for the model fitting.
            is_iplot (bool): If True, plot variable interactions specified via the `i()` syntax.
            rotate_xticks (float): The angle in degrees to rotate the xticks labels. Default is 0 (no rotation).
        Returns:
        None
    """

    if len(models) > 1:

        fig, ax = plt.subplots(len(models), gridspec_kw={
                               'hspace': 0.5}, figsize=figsize)

        for x, model in enumerate(models):

            df_model = df.xs(model)
            coef = df_model["Estimate"].values
            conf_l = coef - \
                df_model["Std. Error"].values * norm.ppf(1 - alpha / 2)
            conf_u = coef + \
                df_model["Std. Error"].values * norm.ppf(1 - alpha / 2)
            coefnames = df_model["coefnames"].values.tolist()

            # could be moved out of the for loop, as the same ivars for all
            # models.

            if is_iplot == True:
                fig.suptitle("iplot")
                coefnames = [(i) for string in coefnames for i in re.findall(
                    r'\[T\.([\d\.\-]+)\]', string)]

            # in the future: add reference category
            # if ref is not None:
            #    coef = np.append(coef, 0)
            #    conf_l = np.append(conf_l, 0)
            #    conf_u = np.append(conf_u, 0)
            #    coefnames = np.append(coefnames, ref)

            ax[x].scatter(coefnames, coef, color="b", alpha=0.8)
            ax[x].scatter(coefnames, conf_u, color="b",
                          alpha=0.8, marker="_", s=100)
            ax[x].scatter(coefnames, conf_l, color="b",
                          alpha=0.8, marker="_", s=100)
            ax[x].vlines(coefnames, ymin=conf_l,
                         ymax=conf_u, color="b", alpha=0.8)
            if yintercept is not None:
                ax[x].axhline(yintercept, color='red',
                              linestyle='--', alpha=0.5)
            if xintercept is not None:
                ax[x].axvline(xintercept, color='red',
                              linestyle='--', alpha=0.5)
            ax[x].set_ylabel('Coefficients')
            ax[x].set_title(model)
            ax[x].tick_params(axis='x', rotation=rotate_xticks)

    else:

        fig, ax = plt.subplots(figsize=figsize)

        model = models[0]

        df_model = df.xs(model)
        coef = df_model["Estimate"].values
        conf_l = coef - df_model["Std. Error"].values * norm.ppf(1 - alpha / 2)
        conf_u = coef + df_model["Std. Error"].values * norm.ppf(1 - alpha / 2)
        coefnames = df_model["coefnames"].values.tolist()

        if is_iplot == True:
            fig.suptitle("iplot")
            coefnames = [(i) for string in coefnames for i in re.findall(
                r'\[T\.([\d\.\-]+)\]', string)]

        # in the future: add reference category
        # if ref is not None:
        #    coef = np.append(coef, 0)
        #    conf_l = np.append(conf_l, 0)
        #    conf_u = np.append(conf_u, 0)
        #    coefnames = np.append(coefnames, ref)

            # c = next(color)

        ax.scatter(coefnames, coef, color="b", alpha=0.8)
        ax.scatter(coefnames, conf_u, color="b", alpha=0.8, marker="_", s=100)
        ax.scatter(coefnames, conf_l, color="b", alpha=0.8, marker="_", s=100)
        ax.vlines(coefnames, ymin=conf_l, ymax=conf_u, color="b", alpha=0.8)
        if yintercept is not None:
            ax.axhline(yintercept, color='red', linestyle='--', alpha=0.5)
        if xintercept is not None:
            ax.axvline(xintercept, color='red', linestyle='--', alpha=0.5)
        ax.set_ylabel('Coefficients')
        ax.set_title(model)
        ax.tick_params(axis='x', rotation=rotate_xticks)

        plt.show()
        plt.close()

def _check_ivars(data, ivars):

    '''
    Checks if the variables in the i() syntax are of the correct type.
    Args:
        data (pandas.DataFrame): The dataframe containing the data used for the model fitting.
        ivars (list): The list of variables specified in the i() syntax.
    Returns:
        None
    '''

    i0_type = data[ivars[0]].dtype
    i1_type = data[ivars[1]].dtype
    if not i0_type in ['category', "O"]:
        raise ValueError("Column " + ivars[0] + " is not of type 'O' or 'category', which is required in the first position of i(). Instead it is of type " +
                        i0_type.name + ". If a reference level is set, it is required that the variable in the first position of 'i()' is of type 'O' or 'category'.")
        if not i1_type in ['int64', 'float64', 'int32', 'float32']:
            raise ValueError("Column " + ivars[1] + " is not of type 'int' or 'float', which is required in the second position of i(). Instead it is of type " +
                            i1_type.name + ". If a reference level is set, iti is required that the variable in the second position of 'i()' is of type 'int' or 'float'.")


def _prepare_split_estimation(split, fsplit, data, var_dict):

    '''
    Cleans the input for the split estimation.
    Checks if the split variables are of the correct type.

    Args:
        split (str): The name of the variable used for the split estimation.
        fsplit (str): The name of the variable used for the fixed split estimation.
        data (pandas.DataFrame): The dataframe containing the data used for the model fitting.
        var_dict (dict): The dictionary containing the variables used in the model.
    Returns:
        splitvar (pandas.Series): The series containing the split variable.
        splitvar_name (str): The name of the split variable. Either equal to split or fsplit.
        estimate_split_model (bool): Whether to estimate the split model.
        estimate_full_model (bool): Whether to estimate the full model.
    '''

    if split is not None:
        if fsplit is not None:
            raise ValueError(
                "Cannot specify both split and fsplit. Please specify only one of the two.")
        else:
            splitvar = data[split]
            estimate_full_model = False
            estimate_split_model = True
            splitvar_name = split
    elif fsplit is not None:
        splitvar = data[fsplit]
        splitvar_name = fsplit
        estimate_full_model = False
        estimate_split_model = True
    else:
        splitvar = None
        splitvar_name = None
        estimate_split_model = False
        estimate_full_model = True


    if splitvar is not None:
        split_categories = np.unique(splitvar)
        if splitvar_name not in data.columns:
            raise ValueError("Split variable " +
                            splitvar + " not found in data.")
        if splitvar_name in var_dict.keys():
            raise ValueError("Split variable " + splitvar +
                            " cannot be a fixed effect variable.")
        if splitvar.dtype.name != "category":
            splitvar = pd.Categorical(splitvar)

    return splitvar, splitvar_name, estimate_split_model, estimate_full_model

def _multicollinearity_checks(X, Z, ivars, fml2):

    '''
    Checks for multicollinearity in the design matrices X and Z.
    Args:
        X (numpy.ndarray): The design matrix X.
        Z (numpy.ndarray): The design matrix (with instruments) Z.
        ivars (list): The list of variables specified in the i() syntax.
        fml2 (str): The formula string.

    '''

    if np.linalg.matrix_rank(X) < min(X.shape):
        if ivars is not None:
            raise ValueError("The design Matrix X does not have full rank for the regression with fml" + fml2 +
                            ". The model is skipped. As you are running a regression via `i()` syntax, maybe you need to drop a level via i(var1, var2, ref = ...)?")
        else:
            raise ValueError(
                    "The design Matrizx X does not have full rank for the regression with fml" + fml2 + ". The model is skipped. ")

    if np.linalg.matrix_rank(Z) < min(Z.shape):
        if ivars is not None:
            raise ValueError("The design Matrix Z does not have full rank for the regression with fml" + fml2 +
                            ". The model is skipped. As you are running a regression via `i()` syntax, maybe you need to drop a level via i(var1, var2, ref = ...)?")
        else:
            raise ValueError(
                    "The design Matrix Z does not have full rank for the regression with fml" + fml2 + ". The model is skipped. ")

def _get_vcov_type(vcov, fval):


    '''
    Passes the specified vcov type. If no vcov type specified, sets the default vcov type as iid if no fixed effect
    is included in the model, and CRV1 clustered by the first fixed effect if a fixed effect is included in the model.
    Args:
        vcov (str): The specified vcov type.
        fval (str): The specified fixed effects. (i.e. "X1+X2")
    Returns:
        vcov_type (str): The specified vcov type.
    '''

    if vcov is None:
        # iid if no fixed effects
        if fval == "0":
            vcov_type = "iid"
        else:
            # CRV1 inference, clustered by first fixed effect
            first_fe = fval.split("+")[0]
            vcov_type = {"CRV1": first_fe}
    else:
        vcov_type = vcov

    return vcov_type


def _clean_ivars(ivars, data):

    '''
    Clean variables interacted via i(X1, X2, ref = a) syntax.

    Args:
        ivars (list): The list of variables specified in the i() syntax.
        data (pandas.DataFrame): The dataframe containing the data used for the model fitting.
    Returns:
        ivars (list): The list of variables specified in the i() syntax minus the reference level
        drop_ref (str): The dropped reference level specified in the i() syntax. None if no level is dropped
    '''

    if ivars is not None:

        if list(ivars.keys())[0] is not None:
            ref = list(ivars.keys())[0]
            ivars = ivars[ref]
            drop_ref = ivars[0] + "[T." + ref + "]" + ":" + ivars[1]
        else:
            ivars = ivars[None]
            drop_ref = None

        # type checking for ivars variable
        _check_ivars(data, ivars)

    else:
        ivars = None
        drop_ref = None

    return ivars, drop_ref

def _drop_singletons(fixef_rm):

    '''
    Checks if the fixef_rm argument is set to "singleton". If so, returns True, else False.
    Args:
        fixef_rm (str): The fixef_rm argument.
    Returns:
        drop_singletons (bool): Whether to drop singletons.
    '''

    if fixef_rm == "singleton":
        return True
    else:
        return False



def _find_untransformed_depvar(transformed_depvar):

    '''
    Args:
        transformed_depvar (str): The transformed depvar

    find untransformed depvar in a formula
    i.e. if "a" is transormed to "log(a)", then "a" is returned
    '''

    match = re.search(r'\((.*?)\)', transformed_depvar)
    if match:
        return match.group(1)
    else:
        return transformed_depvar
