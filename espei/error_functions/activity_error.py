"""
Calculate error due to measured activities.

The residual function implemented in this module needs to exist because it is
currently not possible to compute activity as a property via equilibrium
calculations because as PyCalphad does not yet have a suitable notion of a
reference state that could be used for equilibrium chemical potentials.

"""

import logging
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import numpy.typing as npt
import tinydb
from pycalphad import Database, variables as v
from pycalphad.plot.eqplot import _map_coord_to_variable
from pycalphad.core.utils import filter_phases, unpack_species
from scipy.stats import norm
from pycalphad import Workspace
from pycalphad.property_framework import JanssonDerivative

from espei.core_utils import ravel_conditions
from espei.error_functions.residual_base import ResidualFunction, residual_function_registry
from espei.phase_models import PhaseModelSpecification
from espei.typing import SymbolName
from espei.utils import database_symbols_to_fit, PickleableTinyDB

_log = logging.getLogger(__name__)


def target_chempots_from_activity(component, parameters, target_activity, temperatures, wks_ref):
    """
    Return an array of experimental chemical potentials for the component

    Parameters
    ----------
    component : str
        Name of the component
    target_activity : numpy.ndarray
        Array of experimental activities
    temperatures : numpy.ndarray
        Ravelled array of temperatures (of same size as ``exp_activity``).
    reference_result : xarray.Dataset
        Dataset of the equilibrium reference state. Should contain a singe point calculation.

    Returns
    -------
    numpy.ndarray
        Array of experimental chemical potentials
    """
    
    ref_chempot = wks_ref.get(v.MU(component))
    exp_chem_pots = v.R * temperatures * np.log(target_activity) + ref_chempot
    
    gradient_props = [JanssonDerivative(v.MU(component), key) for key in parameters]
    gradients = wks_ref.get(*gradient_props)
    if type(gradients) is list:
        ref_grads = [float(element) for element in gradients]
    else:
        ref_grads = gradients
    return exp_chem_pots, ref_grads


# TODO: roll this function into ActivityResidual
def calculate_activity_residuals(dbf, comps, phases, datasets, parameters=None, phase_models=None, callables=None, data_weight=1.0) -> Tuple[List[float], List[float], List[float]]:
    """
    Notes
    -----
    General procedure:
    1. Get the datasets
    2. For each dataset

        a. Calculate reference state equilibrium
        b. Calculate current chemical potentials
        c. Find the target chemical potentials
        d. Calculate error due to chemical potentials
    """
    std_dev = 500  # J/mol

    if parameters is None:
        parameters = {}

    params_keys = []

    # This mutates the global pycalphad namespace
    for key in parameters.keys():
        if not hasattr(v, key):
            setattr(v, key, v.IndependentPotential(key))
        params_keys.append(getattr(v, key))
        # Mutates argument to function
        dbf.symbols.pop(key,None)
        
    activity_datasets = datasets.search(
        (tinydb.where('output').test(lambda x: 'ACR' in x)) &
        (tinydb.where('components').test(lambda x: set(x).issubset(comps))))

    residuals = []
    weights = []
    gradients = []
    for ds in activity_datasets:
        acr_component = ds['output'].split('_')[1]  # the component of interest
        # calculate the reference state equilibrium
        ref = ds['reference_state']
        # data_comps and data_phases ensures that we only do calculations on
        # the subsystem of the system defining the data.
        data_comps = ds['components']
        data_phases = filter_phases(dbf, unpack_species(dbf, data_comps), candidate_phases=phases)
        ref_conditions = {_map_coord_to_variable(coord): val for coord, val in ref['conditions'].items()}
        # removed parameter assignment from wks_ref
        ref_conditions.update(parameters)
        wks_ref = Workspace(database=dbf, components=data_comps, phases=ref['phases'], conditions=ref_conditions)

        # calculate current chemical potentials
        # get the conditions
        conditions = {}
        # first make sure the conditions are paired
        # only get the compositions, P and T are special cased
        conds_list = [(cond, value) for cond, value in ds['conditions'].items() if cond not in ('P', 'T')]
        # ravel the conditions
        # we will ravel each composition individually, since they all must have the same shape
        dataset_computed_chempots = []
        dataset_weights = []
        dataset_gradients = []
        for comp_name, comp_x in conds_list:
            P, T, X = ravel_conditions(ds['values'], ds['conditions']['P'], ds['conditions']['T'], comp_x)
            conditions[v.P] = P
            conditions[v.T] = T
            conditions[_map_coord_to_variable(comp_name)] = X
        # do the calculations
        # we cannot currently turn broadcasting off, so we have to do equilibrium one by one
        # invert the conditions dicts to make a list of condition dicts rather than a condition dict of lists
        # assume now that the ravelled conditions all have the same size
        conditions_list = [{c: conditions[c][i] for c in conditions.keys()} for i in range(len(conditions[v.T]))]
        for conds in conditions_list:
            conds.update(parameters)
            wks_sample = Workspace(database=dbf, components=data_comps, phases=data_phases, conditions=conds)
            dataset_computed_chempots.append(wks_sample.get(v.MU(acr_component)))
            dataset_weights.append(std_dev / data_weight / ds.get("weight", 1.0))
            gradient_props = [JanssonDerivative(v.MU(acr_component), key) for key in parameters]
            grads = wks_sample.get(*gradient_props)
            if type(grads) is list:
                sample_grads = [float(element) for element in grads]
            else:
                sample_grads = grads
            dataset_gradients.append(sample_grads)

        # calculate target chempots
        dataset_activities = np.array(ds['values']).flatten()
        dataset_target_chempots, ref_grads = target_chempots_from_activity(acr_component, parameters, dataset_activities, conditions[v.T], wks_ref)
        dataset_residuals = (np.asarray(dataset_computed_chempots) - np.asarray(dataset_target_chempots, dtype=float)).tolist()
        adjusted_gradient = []
        for element in dataset_gradients:
            adjusted_gradient.append((np.asarray(element) - np.asarray(ref_grads)).tolist())
        _log.debug('Data: %s, chemical potential difference: %s, reference: %s', dataset_activities, dataset_residuals, ds["reference"])
        residuals.extend(dataset_residuals)
        weights.extend(dataset_weights)
        gradients.append(adjusted_gradient)
    return residuals, weights, gradients


# TODO: roll this function into ActivityResidual
def calculate_activity_error(dbf, comps, phases, datasets, parameters=None, phase_models=None, callables=None, data_weight=1.0) -> Tuple[float, List[float]]:
    """
    Return the sum of square error from activity data

    Parameters
    ----------
    dbf : pycalphad.Database
        Database to consider
    comps : list
        List of active component names
    phases : list
        List of phases to consider
    datasets : espei.utils.PickleableTinyDB
        Datasets that contain single phase data
    parameters : dict
        Dictionary of symbols that will be overridden in pycalphad.equilibrium
    phase_models : dict
        Phase models to pass to pycalphad calculations
    callables : dict
        Callables to pass to pycalphad
    data_weight : float
        Weight for standard deviation of activity measurements, dimensionless.
        Corresponds to the standard deviation of differences in chemical
        potential in typical measurements of activity, in J/mol.

    Returns
    -------
    float
        A single float of the likelihood


    """
    residuals, weights, gradients = calculate_activity_residuals(dbf, comps, phases, datasets, parameters=parameters, phase_models=phase_models, callables=callables, data_weight=data_weight)
    likelihood = np.sum(norm(0, scale=weights).logpdf(residuals))
    if len(gradients) == 0:
        likelihood_grads = []
    else: 
        gradients = np.concatenate(gradients)
        derivative = -np.array(residuals)*np.array(gradients).T/np.array(weights)**2
        if derivative.ndim == 1:
            likelihood_grads = np.sum(derivative, axis=0)
        else:
            likelihood_grads = np.sum(derivative, axis=1)
    if np.isnan(likelihood):
        # TODO: revisit this case and evaluate whether it is resonable for NaN
        # to show up here. When this comment was written, the test
        # test_subsystem_activity_probability would trigger a NaN.
        return -np.inf, np.zeros(len(parameters))
    return likelihood, likelihood_grads


# TODO: the __init__ method should pre-compute Model and PhaseRecord objects
#       similar to the other residual functions, which will be much more performant.
# TODO: it seems possible (likely?) that "global" callables that were used
#       previously could be incorrect if there are activity datasets with
#       different sets of active components. Usually models, callables, and
#       phase records are tied 1:1 with a set of components. For now, callables
#       will never be built, but this will almost certainly cause a performance
#       regression. Model will also not be pre-built so we can properly use
#       custom user models
class ActivityResidual(ResidualFunction):
    def __init__(
        self,
        database: Database,
        datasets: PickleableTinyDB,
        phase_models: Union[PhaseModelSpecification, None],
        symbols_to_fit: Optional[List[SymbolName]] = None,
        weight: Optional[Dict[str, float]] = None,
        ):
        super().__init__(database, datasets, phase_models, symbols_to_fit, weight)

        if weight is not None:
            self.weight = weight.get("ACR", 1.0)
        else:
            self.weight = 1.0

        if phase_models is not None:
            comps = sorted(phase_models.components)
            model_dict = phase_models.get_model_dict()
        else:
            comps = sorted(database.elements)
            model_dict = dict()
        phases = sorted(filter_phases(database, unpack_species(database, comps), database.phases.keys()))
        if symbols_to_fit is None:
            symbols_to_fit = database_symbols_to_fit(database)
        self._symbols_to_fit = symbols_to_fit

        self._activity_likelihood_kwargs = {
            "dbf": database, "comps": comps, "phases": phases, "datasets": datasets,
            "phase_models": model_dict,
            "callables": None,
            "data_weight": self.weight,
        }

    def get_residuals(self, parameters: npt.ArrayLike) -> Tuple[List[float], List[float]]:
        parameters = {param_name: param for param_name, param in zip(self._symbols_to_fit, parameters.tolist())}
        residuals, weights, grads = calculate_activity_residuals(parameters=parameters, **self._activity_likelihood_kwargs)
        return residuals, weights

    def get_likelihood(self, parameters: npt.NDArray) -> Tuple[float, List[float]]:
        parameters = {param_name: param for param_name, param in zip(self._symbols_to_fit, parameters.tolist())}
        likelihood, gradients = calculate_activity_error(parameters=parameters, **self._activity_likelihood_kwargs)
        return likelihood, gradients


residual_function_registry.register(ActivityResidual)

