# Copyright 2020 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for ferminet.train."""
import itertools
import os

from absl import flags
from absl import logging
from absl.testing import absltest
from absl.testing import parameterized
import chex
from ferminet import base_config
from ferminet import train
from ferminet.configs import atom
from ferminet.configs import diatomic
from ferminet.utils import units
from jax import numpy as jnp
from jax import test_util as jtu
import pyscf

FLAGS = flags.FLAGS
# Default flags are sufficient so mark FLAGS as parsed so we can run the tests
# with py.test, which imports this file rather than runs it.
FLAGS.mark_as_parsed()


def setUpModule():
  # Allow chex_n_cpu_devices to be set via an environment variable as well as
  # --chex_n_cpu_devices to play nicely with pytest.
  fake_devices = os.environ.get('FERMINET_CHEX_N_CPU_DEVICES')
  if fake_devices is not None:
    fake_devices = int(fake_devices)
  try:
    chex.set_n_cpu_devices(n=fake_devices)
  except RuntimeError:
    # jax has already been initialised (e.g. because this is being run with
    # other tests via a test runner such as pytest)
    logging.info('JAX already initialised so cannot set number of CPU devices. '
                 'Using a single device in train_test.')


def _config_params():
  for params in itertools.product(('Li', 'LiH'), ('kfac', 'adam')):
    yield params
  for optimizer in ('kfac', 'adam'):
    yield ('H', optimizer)
  yield ('Li', 'lamb')


class QmcTest(jtu.JaxTestCase):

  def setUp(self):
    super(QmcTest, self).setUp()
    # disable use of temp directory in pyscf.
    # Test calculations are small enough to fit in RAM and we don't need
    # checkpoint files.
    pyscf.lib.param.TMPDIR = None

  @parameterized.parameters(_config_params())
  def test_training_step(self, system, optimizer):
    if system in ('H', 'Li'):
      cfg = atom.get_config()
      cfg.system.atom = system
    else:
      cfg = diatomic.get_config()
      cfg.system.molecule_name = system
    cfg.network.detnet.hidden_dims = ((16, 4),) * 2
    cfg.network.detnet.determinants = 2
    cfg.batch_size = 32
    cfg.pretrain.iterations = 10
    cfg.mcmc.burn_in = 10
    cfg.optim.optimizer = optimizer
    cfg.optim.iterations = 3
    cfg.debug.check_nan = True
    cfg.log.save_path = self.create_tempdir().full_path
    cfg = base_config.resolve(cfg)
    # Calculation is too small to test the results for accuracy. Test just to
    # ensure they actually run without a top-level error.
    train.train(cfg)


MOL_STRINGS = [
    'H 0 0 -1; H 0 0 1', 'O 0 0 0; H  0 1 0; H 0 0 1', 'H 0 0 0; Cl 0 0 1.1',
]


class QmcPyscfMolTest(jtu.JaxTestCase):

  def setUp(self):
    super(QmcPyscfMolTest, self).setUp()
    # disable use of temp directory in pyscf.
    # Test calculations are small enough to fit in RAM and we don't need
    # checkpoint files.
    pyscf.lib.param.TMPDIR = None

  @parameterized.parameters(
      (mol_string, optimizer)
      for mol_string, optimizer in zip(MOL_STRINGS[:2], ('adam', 'kfac')))
  def test_training_step_pyscf(self, mol_string, optimizer):
    mol = pyscf.gto.Mole()
    mol.build(
        atom=mol_string,
        basis='sto-3g', unit='bohr')
    cfg = base_config.default()
    cfg.system.pyscf_mol = mol

    cfg.network.detnet.hidden_dims = ((16, 4),) * 2
    cfg.network.detnet.determinants = 2
    cfg.batch_size = 32
    cfg.pretrain.iterations = 10
    cfg.mcmc.burn_in = 10
    cfg.optim.optimizer = optimizer
    cfg.optim.iterations = 3
    cfg.debug.check_nan = True
    cfg.log.save_path = self.create_tempdir().full_path
    cfg = base_config.resolve(cfg)
    # Calculation is too small to test the results for accuracy. Test just to
    # ensure they actually run without a top-level error.
    train.train(cfg)

  @parameterized.parameters(MOL_STRINGS)
  def test_conversion_pyscf(self, mol_string):
    mol = pyscf.gto.Mole()
    mol.build(
        atom=mol_string,
        basis='sto-3g', unit='bohr')
    cfg = base_config.default()
    cfg.system.pyscf_mol = mol
    cfg = train.pyscf_to_molecule(cfg)
    # Assert that the alpha and beta electrons are the same
    self.assertEqual(mol.nelec, cfg.system.electrons)
    # Assert that the basis are the same
    self.assertEqual(mol.basis, cfg.pretrain.basis)
    # Assert that atom symbols are the same
    self.assertEqual([mol.atom_symbol(i) for i in range(mol.natm)],
                     [atom.symbol for atom in cfg.system.molecule])
    # Assert that atom coordinates are the same
    pyscf_coords = [mol.atom_coords()[i] for i in range(mol.natm)]
    internal_coords = [jnp.array(atom.coords) for atom in cfg.system.molecule]
    self.assertAllClose(pyscf_coords, internal_coords)

  def test_conversion_pyscf_ang(self):
    mol = pyscf.gto.Mole()
    mol.build(
        atom='H 0 0 -1; H 0 0 1',
        basis='sto-3g', unit='ang')
    cfg = base_config.default()
    cfg.system.pyscf_mol = mol
    cfg = train.pyscf_to_molecule(cfg)
    # Assert that the coordinates are now in bohr internally
    bohr_coords = [[0, 0, -units.BOHR_ANGSTROM], [0, 0, units.BOHR_ANGSTROM]]
    self.assertAllClose([atom.coords for atom in cfg.system.molecule],
                        bohr_coords,
                        check_dtypes=False)
    # Assert that the alpha and beta electrons are the same
    self.assertEqual(mol.nelec, cfg.system.electrons)
    # Assert that the basis are the same
    self.assertEqual(mol.basis, cfg.pretrain.basis)
    # Assert that atom symbols are the same
    self.assertEqual([mol.atom_symbol(i) for i in range(mol.natm)],
                     [atom.symbol for atom in cfg.system.molecule])
    # Assert that atom coordinates are the same
    pyscf_coords = [mol.atom_coords()[i] for i in range(mol.natm)]
    internal_coords = [jnp.array(atom.coords) for atom in cfg.system.molecule]
    self.assertAllClose(pyscf_coords, internal_coords)

if __name__ == '__main__':
  absltest.main()
