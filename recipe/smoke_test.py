"""Small integration checks for the installed Conda package and its dependencies."""

import csv
from importlib import metadata
import os
import sys
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

import numpy as np
import parmed
from packaging.requirements import Requirement


def run(command, directory, environment):
    print('+', ' '.join(map(str, command)), flush=True)
    result = subprocess.run(command, cwd=directory, env=environment, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    print(result.stdout, flush=True)
    if result.returncode:
        raise RuntimeError(f'{command[0]} failed with exit code {result.returncode}')
    return result.stdout


def main():
    # AmberTools bundles optional utilities with incomplete Python dependency
    # metadata. Validate this distribution's requirements rather than failing
    # on unrelated utilities from a dependency's installation.
    for declaration in metadata.requires('gmx_MMPBSA') or ():
        requirement = Requirement(declaration)
        if requirement.marker is None or requirement.marker.evaluate():
            installed = metadata.version(requirement.name)
            assert installed in requirement.specifier, f'{requirement}: found {installed}'

    environment = os.environ.copy()
    environment.update(OMP_NUM_THREADS='1', QT_QPA_PLATFORM='offscreen')
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        environment.update(OMPI_ALLOW_RUN_AS_ROOT='1', OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
    required = ('tleap', 'cpptraj', 'sander', 'gmx', 'mpirun',
                'gmx_MMPBSA', 'amber_MMPBSA', 'gmx_MMPBSA_ana')
    for name in required:
        assert shutil.which(name), f'Missing dependency executable: {name}'

    with TemporaryDirectory(prefix='gmx-mmpbsa-conda-test-') as directory:
        root = Path(directory)
        inputs = root / 'inputs'
        inputs.mkdir()
        (inputs / 'leap.in').write_text('''source leaprc.protein.ff14SB
set default PBRadii mbondi2
receptor = sequence { ACE ALA NME }
ligand = sequence { ACE GLY NME }
translate ligand { 10.0 0.0 0.0 }
complex = combine { receptor ligand }
set complex box { 40.0 40.0 40.0 }
saveamberparm complex complex.prmtop complex.rst7
quit
''')
        run(['tleap', '-f', 'leap.in'], inputs, environment)
        topology = parmed.load_file(str(inputs / 'complex.prmtop'),
                                   xyz=str(inputs / 'complex.rst7'))
        topology.save(str(inputs / 'complex.pdb'), overwrite=True)
        topology.save(str(inputs / 'complex.top'), overwrite=True)
        receptor = [atom.idx + 1 for residue in topology.residues[:3] for atom in residue.atoms]
        ligand = [atom.idx + 1 for residue in topology.residues[3:] for atom in residue.atoms]
        (inputs / 'index.ndx').write_text(
            '[ receptor ]\n' + ' '.join(map(str, receptor)) + '\n[ ligand ]\n'
            + ' '.join(map(str, ligand)) + '\n')
        (inputs / 'trajectory.in').write_text('''trajin complex.rst7
trajin complex.rst7
trajout complex.xtc
run
quit
''')
        run(['cpptraj', '-p', 'complex.prmtop', '-i', 'trajectory.in'], inputs, environment)

        outputs = {}
        for case in ('native_serial', 'gromacs_serial', 'native_mpi'):
            work = root / case
            work.mkdir()
            (work / 'mmpbsa.in').write_text('''&general
startframe=1, endframe=2, PBRadii=3,
/
&gb
igb=5, saltcon=0.150,
/
''')
            if case == 'gromacs_serial':
                command = ['gmx_MMPBSA', '-cs', str(inputs / 'complex.pdb'),
                           '-cp', str(inputs / 'complex.top'), '-ct', str(inputs / 'complex.xtc'),
                           '-ci', str(inputs / 'index.ndx'), '-cg', 'receptor', 'ligand']
            else:
                command = ['amber_MMPBSA', '-cp', str(inputs / 'complex.prmtop'),
                           '-ct', str(inputs / 'complex.xtc'), '-cm', ':1-3', ':4-6']
                if case == 'native_mpi':
                    command = ['mpirun', '-np', '2', command[0], 'MPI', *command[1:]]
            run([*command, '-O', '-i', 'mmpbsa.in', '-nogui'], work, environment)
            report = work / 'FINAL_RESULTS_MMPBSA.dat'
            energies = work / 'FINAL_RESULTS_MMPBSA.csv'
            assert report.is_file() and report.stat().st_size
            assert energies.is_file() and energies.stat().st_size
            # Every numerical energy row must contain finite values.
            with energies.open() as data:
                rows = [row for row in csv.reader(data) if row and row[0] in ('1', '2')]
            assert len(rows) == 8, f'{case}: expected two frames for each of four energy sections'
            assert all(np.isfinite(float(value)) for row in rows for value in row)
            outputs[case] = energies.read_bytes()
        assert outputs['native_serial'] == outputs['native_mpi'], 'Serial/MPI energy mismatch'

        run(['gmx_MMPBSA_ana', '-h'], root, environment)
        run([sys.executable, '-c',
             'from PyQt6.QtWidgets import QApplication; '
             'app = QApplication([]); assert app.platformName() == "offscreen"'], root, environment)
    print('PASS: native AMBER, GROMACS, two-rank MPI parity, and offscreen Qt startup', flush=True)


if __name__ == '__main__':
    main()
