# This code is part of Qiskit.
#
# (C) Copyright IBM 2021, 2022.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""The Active-Space Reduction interface."""

import logging

from copy import deepcopy
from typing import List, Optional, Tuple, Union, cast

import numpy as np

from qiskit_nature import QiskitNatureError
from qiskit_nature.properties import GroupedProperty, Property
from qiskit_nature.properties.second_quantization import (
    SecondQuantizedProperty,
    GroupedSecondQuantizedProperty,
)
from qiskit_nature.properties.second_quantization.driver_metadata import DriverMetadata
from qiskit_nature.properties.second_quantization.electronic import ParticleNumber
from qiskit_nature.properties.second_quantization.electronic.bases import (
    ElectronicBasis,
    ElectronicBasisTransform,
)
from qiskit_nature.properties.second_quantization.electronic.electronic_structure_driver_result import (
    ElectronicStructureDriverResult,
)
from qiskit_nature.properties.second_quantization.electronic.integrals import (
    IntegralProperty,
    OneBodyElectronicIntegrals,
)
from qiskit_nature.properties.second_quantization.electronic.types import GroupedElectronicProperty
from qiskit_nature.results import ElectronicStructureResult

from ..base_transformer import BaseTransformer

logger = logging.getLogger(__name__)


class ActiveSpaceTransformer(BaseTransformer):
    r"""The Active-Space reduction.

    The reduction is done by computing the inactive Fock operator which is defined as
    :math:`F^I_{pq} = h_{pq} + \sum_i 2 g_{iipq} - g_{iqpi}` and the inactive energy which is
    given by :math:`E^I = \sum_j h_{jj} + F ^I_{jj}`, where :math:`i` and :math:`j` iterate over
    the inactive orbitals.
    By using the inactive Fock operator in place of the one-electron integrals, `h1`, the
    description of the active space contains an effective potential generated by the inactive
    electrons. Therefore, this method permits the exclusion of non-core electrons while
    retaining a high-quality description of the system.

    For more details on the computation of the inactive Fock operator refer to
    https://arxiv.org/abs/2009.01872.

    The active space can be configured in one of the following ways through the initializer:
        - when only `num_electrons` and `num_molecular_orbitals` are specified, these integers
          indicate the number of active electrons and orbitals, respectively. The active space will
          then be chosen around the Fermi level resulting in a unique choice for any pair of
          numbers.  Nonetheless, the following criteria must be met:

            #. the remaining number of inactive electrons must be a positive, even number

            #. the number of active orbitals must not exceed the total number of orbitals minus the
               number of orbitals occupied by the inactive electrons

        - when, in addition to the above, `num_alpha` is specified, this can be used to disambiguate
          the active space in systems with non-zero spin. Thus, `num_alpha` determines the number of
          active alpha electrons. The number of active beta electrons can then be determined based
          via `num_beta = num_electrons - num_alpha`. The same requirements as listed in the
          previous case must be met.
        - finally, it is possible to select a custom set of active orbitals via their indices using
          `active_orbitals`. This allows selecting an active space which is not placed around the
          Fermi level as described in the first case, above. When using this keyword argument, the
          following criteria must be met *in addition* to the ones listed above:

            #. the length of `active_orbitals` must be equal to `num_molecular_orbitals`.

            #. the sum of electrons present in `active_orbitals` must be equal to `num_electrons`.

    References:
        - *M. Rossmannek, P. Barkoutsos, P. Ollitrault, and I. Tavernelli, arXiv:2009.01872
          (2020).*
    """

    def __init__(
        self,
        num_electrons: Optional[Union[int, Tuple[int, int]]] = None,
        num_molecular_orbitals: Optional[int] = None,
        active_orbitals: Optional[List[int]] = None,
    ):
        """Initializes a transformer which can reduce a `GroupedElectronicProperty` to a configured
        active space.

        This transformer requires a `ParticleNumber` property and an `ElectronicBasisTransform`
        pseudo-property to be available as well as `ElectronicIntegrals` in the `ElectronicBasis.AO`
        basis. An `ElectronicStructureDriverResult` produced by Qiskit's drivers in general
        satisfies these conditions unless it was read from an FCIDump file. However, those integrals
        are likely already reduced by the code which produced the file.

        Args:
            num_electrons: The number of active electrons. If this is a tuple, it represents the
                           number of alpha and beta electrons. If this is a number, it is
                           interpreted as the total number of active electrons, should be even, and
                           implies that the number of alpha and beta electrons equals half of this
                           value, respectively.
            num_molecular_orbitals: The number of active orbitals.
            active_orbitals: A list of indices specifying the molecular orbitals of the active
                             space. This argument must match with the remaining arguments and should
                             only be used to enforce an active space that is not chosen purely
                             around the Fermi level.

        Raises:
            QiskitNatureError: if an invalid configuration is provided.
        """
        self._num_electrons = num_electrons
        self._num_molecular_orbitals = num_molecular_orbitals
        self._active_orbitals = active_orbitals

        try:
            self._check_configuration()
        except QiskitNatureError as exc:
            raise QiskitNatureError("Incorrect Active-Space configuration.") from exc

        self._mo_occ_total: np.ndarray = None
        self._active_orbs_indices: List[int] = None
        self._transform_active: ElectronicBasisTransform = None
        self._density_inactive: OneBodyElectronicIntegrals = None

    def _check_configuration(self):
        if isinstance(self._num_electrons, int):
            if self._num_electrons % 2 != 0:
                raise QiskitNatureError(
                    "The number of active electrons must be even! Otherwise you must specify them "
                    "as a tuple, not as:",
                    str(self._num_electrons),
                )
            if self._num_electrons < 0:
                raise QiskitNatureError(
                    "The number of active electrons cannot be negative, not:",
                    str(self._num_electrons),
                )
        elif isinstance(self._num_electrons, tuple):
            if not all(isinstance(n_elec, int) and n_elec >= 0 for n_elec in self._num_electrons):
                raise QiskitNatureError(
                    "Neither the number of alpha, nor the number of beta electrons can be "
                    "negative, not:",
                    str(self._num_electrons),
                )
        else:
            raise QiskitNatureError(
                "The number of active electrons must be an int, or a tuple thereof, not:",
                str(self._num_electrons),
            )

        if isinstance(self._num_molecular_orbitals, int):
            if self._num_molecular_orbitals < 0:
                raise QiskitNatureError(
                    "The number of active orbitals cannot be negative, not:",
                    str(self._num_molecular_orbitals),
                )
        else:
            raise QiskitNatureError(
                "The number of active orbitals must be an int, not:",
                str(self._num_electrons),
            )

    def transform(
        self, grouped_property: GroupedSecondQuantizedProperty
    ) -> GroupedElectronicProperty:
        """Reduces the given `GroupedElectronicProperty` to a given active space.

        Args:
            grouped_property: the `GroupedElectronicProperty` to be transformed.

        Returns:
            A new `GroupedElectronicProperty` instance.

        Raises:
            QiskitNatureError: If the provided `GroupedElectronicProperty` does not contain a
                               `ParticleNumber` or `ElectronicBasisTransform` instance, if more
                               electrons or orbitals are requested than are available, or if the
                               number of selected active orbital indices does not match
                               `num_molecular_orbitals`.
        """
        if not isinstance(grouped_property, GroupedElectronicProperty):
            raise QiskitNatureError(
                "Only `GroupedElectronicProperty` objects can be transformed by this Transformer, "
                f"not objects of type, {type(grouped_property)}."
            )

        particle_number = grouped_property.get_property(ParticleNumber)
        if particle_number is None:
            raise QiskitNatureError(
                "The provided `GroupedElectronicProperty` does not contain a `ParticleNumber` "
                "property, which is required by this transformer!"
            )
        particle_number = cast(ParticleNumber, particle_number)

        electronic_basis_transform = grouped_property.get_property(ElectronicBasisTransform)
        if electronic_basis_transform is None:
            raise QiskitNatureError(
                "The provided `GroupedElectronicProperty` does not contain an "
                "`ElectronicBasisTransform` property, which is required by this transformer!"
            )
        electronic_basis_transform = cast(ElectronicBasisTransform, electronic_basis_transform)

        # get molecular orbital occupation numbers
        occupation_alpha = particle_number.occupation_alpha
        occupation_beta = particle_number.occupation_beta
        self._mo_occ_total = occupation_alpha + occupation_beta

        # determine the active space
        self._active_orbs_indices, inactive_orbs_idxs = self._determine_active_space(
            grouped_property
        )

        # get molecular orbital coefficients
        coeff_alpha = electronic_basis_transform.coeff_alpha
        coeff_beta = electronic_basis_transform.coeff_beta

        # initialize size-reducing basis transformation
        self._transform_active = ElectronicBasisTransform(
            ElectronicBasis.AO,
            ElectronicBasis.MO,
            coeff_alpha[:, self._active_orbs_indices],
            coeff_beta[:, self._active_orbs_indices],
        )

        # compute inactive density matrix
        def _inactive_density(mo_occ, mo_coeff):
            return np.dot(
                mo_coeff[:, inactive_orbs_idxs] * mo_occ[inactive_orbs_idxs],
                np.transpose(mo_coeff[:, inactive_orbs_idxs]),
            )

        self._density_inactive = OneBodyElectronicIntegrals(
            ElectronicBasis.AO,
            (
                _inactive_density(occupation_alpha, coeff_alpha),
                _inactive_density(occupation_beta, coeff_beta),
            ),
        )

        # construct new GroupedElectronicProperty
        grouped_property_transformed = ElectronicStructureResult()
        grouped_property_transformed.electronic_basis_transform = self._transform_active
        grouped_property_transformed = self._transform_property(grouped_property)  # type: ignore

        return grouped_property_transformed

    def _determine_active_space(
        self, grouped_property: GroupedElectronicProperty
    ) -> Tuple[List[int], List[int]]:
        """Determines the active and inactive orbital indices.

        Args:
            grouped_property: the `GroupedElectronicProperty` to be transformed.

        Returns:
            The list of active and inactive orbital indices.
        """
        particle_number = grouped_property.get_property(ParticleNumber)
        if isinstance(self._num_electrons, tuple):
            num_alpha, num_beta = self._num_electrons
        elif isinstance(self._num_electrons, int):
            num_alpha = num_beta = self._num_electrons // 2

        # compute number of inactive electrons
        nelec_total = particle_number._num_alpha + particle_number._num_beta
        nelec_inactive = nelec_total - num_alpha - num_beta

        self._validate_num_electrons(nelec_inactive)
        self._validate_num_orbitals(nelec_inactive, particle_number)

        # determine active and inactive orbital indices
        if self._active_orbitals is None:
            norbs_inactive = nelec_inactive // 2
            inactive_orbs_idxs = list(range(norbs_inactive))
            active_orbs_idxs = list(
                range(norbs_inactive, norbs_inactive + self._num_molecular_orbitals)
            )
        else:
            active_orbs_idxs = self._active_orbitals
            inactive_orbs_idxs = [
                o
                for o in range(nelec_total // 2)
                if o not in self._active_orbitals and self._mo_occ_total[o] > 0
            ]

        return (active_orbs_idxs, inactive_orbs_idxs)

    def _validate_num_electrons(self, nelec_inactive: int) -> None:
        """Validates the number of electrons.

        Args:
            nelec_inactive: the computed number of inactive electrons.

        Raises:
            QiskitNatureError: if the number of inactive electrons is either negative or odd.
        """
        if nelec_inactive < 0:
            raise QiskitNatureError("More electrons requested than available.")
        if nelec_inactive % 2 != 0:
            raise QiskitNatureError("The number of inactive electrons must be even.")

    def _validate_num_orbitals(self, nelec_inactive: int, particle_number: ParticleNumber) -> None:
        """Validates the number of orbitals.

        Args:
            nelec_inactive: the computed number of inactive electrons.
            particle_number: the `ParticleNumber` containing system size information.

        Raises:
            QiskitNatureError: if more orbitals were requested than are available in total or if the
                               number of selected orbitals mismatches the specified number of active
                               orbitals.
        """
        if self._active_orbitals is None:
            norbs_inactive = nelec_inactive // 2
            if (
                norbs_inactive + self._num_molecular_orbitals
                > particle_number._num_spin_orbitals // 2
            ):
                raise QiskitNatureError("More orbitals requested than available.")
        else:
            if self._num_molecular_orbitals != len(self._active_orbitals):
                raise QiskitNatureError(
                    "The number of selected active orbital indices does not "
                    "match the specified number of active orbitals."
                )
            if max(self._active_orbitals) >= particle_number._num_spin_orbitals // 2:
                raise QiskitNatureError("More orbitals requested than available.")
            expected_num_electrons = (
                self._num_electrons
                if isinstance(self._num_electrons, int)
                else sum(self._num_electrons)
            )
            if sum(self._mo_occ_total[self._active_orbitals]) != expected_num_electrons:
                raise QiskitNatureError(
                    "The number of electrons in the selected active orbitals "
                    "does not match the specified number of active electrons."
                )

    # TODO: can we efficiently extract this into the base class? At least the logic dealing with
    # recursion is general and we should avoid having to duplicate it.
    def _transform_property(self, prop: Property) -> Property:
        """Transforms a Property object.

        This is a recursive reduction, iterating GroupedProperty objects when encountering one.

        Args:
            property: the property object to transform.

        Returns:
            The transformed property object.

        Raises:
            TypeError: if an unexpected Property subtype is encountered.
        """
        transformed_property: Property
        if isinstance(prop, GroupedProperty):
            transformed_property = prop.__class__()  # type: ignore[call-arg]
            transformed_property.name = prop.name

            if isinstance(prop, ElectronicStructureDriverResult):
                transformed_property.molecule = prop.molecule  # type: ignore[attr-defined]

            for internal_property in iter(prop):
                try:
                    transformed_internal_property = self._transform_property(internal_property)
                    if transformed_internal_property is not None:
                        transformed_property.add_property(transformed_internal_property)
                except TypeError:
                    logger.warning(
                        "The Property %s of type %s could not be transformed!",
                        internal_property.name,
                        type(internal_property),
                    )
                    continue

            # Removing empty GroupedProperty
            if len(transformed_property._properties) == 0:
                transformed_property = None

        elif isinstance(prop, IntegralProperty):
            # get matrix operator of IntegralProperty
            fock_operator = prop.integral_operator(self._density_inactive)
            # the total operator equals the AO-1-body-term + the inactive matrix operator
            total_op = prop.get_electronic_integral(ElectronicBasis.AO, 1) + fock_operator
            # compute the energy shift introduced by the ActiveSpaceTransformer
            e_inactive = 0.5 * cast(complex, total_op.compose(self._density_inactive))

            transformed_property = deepcopy(prop)
            # insert the AO-basis inactive operator
            transformed_property.add_electronic_integral(fock_operator)
            # actually reduce the system size
            transformed_property.transform_basis(self._transform_active)
            # insert the energy shift
            transformed_property._shift[self.__class__.__name__] = e_inactive

        elif isinstance(prop, ParticleNumber):
            p_n = prop
            active_occ_alpha = p_n.occupation_alpha[self._active_orbs_indices]
            active_occ_beta = p_n.occupation_beta[self._active_orbs_indices]
            transformed_property = ParticleNumber(
                len(self._active_orbs_indices) * 2,
                (int(sum(active_occ_alpha)), int(sum(active_occ_beta))),
                active_occ_alpha,
                active_occ_beta,
            )

        elif isinstance(prop, SecondQuantizedProperty):
            transformed_property = prop.__class__(len(self._active_orbs_indices) * 2)  # type: ignore

        elif isinstance(prop, ElectronicBasisTransform):
            # transformation done manually during `transform`
            transformed_property = prop

        elif isinstance(prop, DriverMetadata):
            # for the time being we manually catch this to avoid unnecessary warnings
            # TODO: support storing transformer information in the DriverMetadata container
            transformed_property = prop

        else:
            raise TypeError(f"{type(prop)} is an unsupported Property-type for this Transformer!")

        return transformed_property
