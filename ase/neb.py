# -*- coding: utf-8 -*-
import threading
from math import sqrt

import numpy as np

import ase.parallel as mpi
from ase.calculators.calculator import Calculator
from ase.calculators.singlepoint import SinglePointCalculator
from ase.io import read
from ase.optimize import BFGS
from ase.utils.geometry import find_mic


class NEB:
    def __init__(self, images, k=0.1, climb=False, parallel=False,
                 world=None):
        """Nudged elastic band.

        images: list of Atoms objects
            Images defining path from initial to final state.
        k: float or list of floats
            Spring constant(s) in eV/Ang.  One number or one for each spring.
        climb: bool
            Use a climbing image (default is no climbing image).
        parallel: bool
            Distribute images over processors.
        """
        self.images = images
        self.climb = climb
        self.parallel = parallel
        self.natoms = len(images[0])
        self.nimages = len(images)
        self.emax = np.nan

        if isinstance(k, (float, int)):
            k = [k] * (self.nimages - 1)
        self.k = list(k)

        if world is None:
            world = mpi.world
        self.world = world

        if parallel:
            assert world.size == 1 or world.size % (self.nimages - 2) == 0

    def interpolate(self, method='linear'):
        interpolate(self.images)

        if method == 'idpp':
            self.idpp_interpolate(traj=None, log=None)
            
    def idpp_interpolate(self, traj='idpp.traj', log='idpp.log', fmax=0.1,
                         optimizer=BFGS):
        d1 = self.images[0].get_all_distances()
        d2 = self.images[-1].get_all_distances()
        d = (d2 - d1) / (self.nimages - 1)
        old = []
        for i, image in enumerate(self.images):
            old.append(image.calc)
            image.calc = IDPP(d1 + i * d)
        opt = BFGS(self, trajectory=traj, logfile=log)
        opt.run(fmax=0.1)
        for image, calc in zip(self.images, old):
            image.calc = calc
        
    def get_positions(self):
        positions = np.empty(((self.nimages - 2) * self.natoms, 3))
        n1 = 0
        for image in self.images[1:-1]:
            n2 = n1 + self.natoms
            positions[n1:n2] = image.get_positions()
            n1 = n2
        return positions

    def set_positions(self, positions):
        n1 = 0
        for image in self.images[1:-1]:
            n2 = n1 + self.natoms
            image.set_positions(positions[n1:n2])
            n1 = n2

            # Parallel NEB with Jacapo needs this:
            try:
                image.get_calculator().set_atoms(image)
            except AttributeError:
                pass
        
    def get_forces(self):
        """Evaluate and return the forces."""
        images = self.images
        forces = np.empty(((self.nimages - 2), self.natoms, 3))
        energies = np.empty(self.nimages - 2)

        if not self.parallel:
            # Do all images - one at a time:
            for i in range(1, self.nimages - 1):
                energies[i - 1] = images[i].get_potential_energy()
                forces[i - 1] = images[i].get_forces()
        elif self.world.size == 1:
            def run(image, energies, forces):
                energies[:] = image.get_potential_energy()
                forces[:] = image.get_forces()
            threads = [threading.Thread(target=run,
                                        args=(images[i],
                                              energies[i - 1:i],
                                              forces[i - 1:i]))
                       for i in range(1, self.nimages - 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        else:
            # Parallelize over images:
            i = self.world.rank * (self.nimages - 2) // self.world.size + 1
            try:
                energies[i - 1] = images[i].get_potential_energy()
                forces[i - 1] = images[i].get_forces()
            except:
                # Make sure other images also fail:
                error = self.world.sum(1.0)
                raise
            else:
                error = self.world.sum(0.0)
                if error:
                    raise RuntimeError('Parallel NEB failed!')
                
            for i in range(1, self.nimages - 1):
                root = (i - 1) * self.world.size // (self.nimages - 2)
                self.world.broadcast(energies[i - 1:i], root)
                self.world.broadcast(forces[i - 1], root)

        imax = 1 + np.argsort(energies)[-1]
        self.emax = energies[imax - 1]
        
        tangent1 = images[1].get_positions() - images[0].get_positions()
        for i in range(1, self.nimages - 1):
            tangent2 = (images[i + 1].get_positions() -
                        images[i].get_positions())
            if i < imax:
                tangent = tangent2
            elif i > imax:
                tangent = tangent1
            else:
                tangent = tangent1 + tangent2
                
            tt = np.vdot(tangent, tangent)
            f = forces[i - 1]
            ft = np.vdot(f, tangent)
            if i == imax and self.climb:
                f -= 2 * ft / tt * tangent
            else:
                f -= ft / tt * tangent
                f -= np.vdot(tangent1 * self.k[i - 1] -
                             tangent2 * self.k[i], tangent) / tt * tangent
                
            tangent1 = tangent2

        return forces.reshape((-1, 3))

    def get_potential_energy(self):
        return self.emax

    def __len__(self):
        return (self.nimages - 2) * self.natoms


class IDPP(Calculator):
    """Image dependent pair potential.

    See:

        Improved initial guess for minimum energy path calculations.

        Søren Smidstrup, Andreas Pedersen, Kurt Stokbro and Hannes Jónsson

        Chem. Phys. 140, 214106 (2014)
    """

    implemented_properties = ['energy', 'forces']

    def __init__(self, target):
        Calculator.__init__(self)
        self.target = target

    def calculate(self, atoms, properties, system_changes):
        Calculator.calculate(self, atoms, properties, system_changes)

        P = atoms.positions
        D = np.array([P - p for p in P])  # all distance vectors
        d = (D**2).sum(2)**0.5
        dd = d - self.target
        d.ravel()[::len(d) + 1] = 1  # avoid dividing by zero
        d4 = d**4
        e = 0.5 * (dd**2 / d4).sum()
        f = -2 * ((dd * (1 - 2 * dd / d) / d**5)[..., np.newaxis] * D).sum(0)
        self.results = {'energy': e, 'forces': f}


class SingleCalculatorNEB(NEB):
    def __init__(self, images, k=0.1, climb=False):
        if isinstance(images, str):
            # this is a filename
            traj = read(images, '0:')
            images = []
            for atoms in traj:
                images.append(atoms)

        NEB.__init__(self, images, k, climb, False)
        self.calculators = [None] * self.nimages
        self.energies_ok = False
        self.first = True
 
    def interpolate(self, initial=0, final=-1, mic=False):
        """Interpolate linearly between initial and final images."""
        if final < 0:
            final = self.nimages + final
        n = final - initial
        pos1 = self.images[initial].get_positions()
        pos2 = self.images[final].get_positions()
        dist = (pos2 - pos1)
        if mic:
            cell = self.images[initial].get_cell()
            assert((cell == self.images[final].get_cell()).all())
            pbc = self.images[initial].get_pbc()
            assert((pbc == self.images[final].get_pbc()).all())
            dist, D_len = find_mic(dist, cell, pbc)
        dist /= n
        for i in range(1, n):
            self.images[initial + i].set_positions(pos1 + i * dist)

    def refine(self, steps=1, begin=0, end=-1, mic=False):
        """Refine the NEB trajectory."""
        if end < 0:
            end = self.nimages + end
        j = begin
        n = end - begin
        for i in range(n):
            for k in range(steps):
                self.images.insert(j + 1, self.images[j].copy())
                self.calculators.insert(j + 1, None)
            self.k[j:j + 1] = [self.k[j] * (steps + 1)] * (steps + 1)
            self.nimages = len(self.images)
            self.interpolate(j, j + steps + 1, mic=mic)
            j += steps + 1

    def set_positions(self, positions):
        # new positions -> new forces
        if self.energies_ok:
            # restore calculators
            self.set_calculators(self.calculators[1:-1])
        NEB.set_positions(self, positions)

    def get_calculators(self):
        """Return the original calculators."""
        calculators = []
        for i, image in enumerate(self.images):
            if self.calculators[i] is None:
                calculators.append(image.get_calculator())
            else:
                calculators.append(self.calculators[i])
        return calculators
    
    def set_calculators(self, calculators):
        """Set new calculators to the images."""
        self.energies_ok = False
        self.first = True

        if not isinstance(calculators, list):
            calculators = [calculators] * self.nimages

        n = len(calculators)
        if n == self.nimages:
            for i in range(self.nimages):
                self.images[i].set_calculator(calculators[i])
        elif n == self.nimages - 2:
            for i in range(1, self.nimages - 1):
                self.images[i].set_calculator(calculators[i - 1])
        else:
            raise RuntimeError(
                'len(calculators)=%d does not fit to len(images)=%d'
                % (n, self.nimages))

    def get_energies_and_forces(self):
        """Evaluate energies and forces and hide the calculators"""
        if self.energies_ok:
            return

        self.emax = -1.e32

        def calculate_and_hide(i):
            image = self.images[i]
            calc = image.get_calculator()
            if self.calculators[i] is None:
                self.calculators[i] = calc
            if calc is not None:
                if not isinstance(calc, SinglePointCalculator):
                    self.images[i].set_calculator(
                        SinglePointCalculator(
                            image,
                            energy=image.get_potential_energy(),
                            forces=image.get_forces()))
                self.emax = min(self.emax, image.get_potential_energy())

        if self.first:
            calculate_and_hide(0)

        # Do all images - one at a time:
        for i in range(1, self.nimages - 1):
            calculate_and_hide(i)

        if self.first:
            calculate_and_hide(-1)
            self.first = False

        self.energies_ok = True
       
    def get_forces(self):
        self.get_energies_and_forces()
        return NEB.get_forces(self)

    def n(self):
        return self.nimages

    def write(self, filename):
        from ase.io.trajectory import Trajectory
        traj = Trajectory(filename, 'w', self)
        traj.write()
        traj.close()

    def __add__(self, other):
        for image in other:
            self.images.append(image)
        return self


def fit0(E, F, R):
    """Constructs curve parameters from the NEB images."""
    E = np.array(E) - E[0]
    n = len(E)
    Efit = np.empty((n - 1) * 20 + 1)
    Sfit = np.empty((n - 1) * 20 + 1)

    s = [0]
    for i in range(n - 1):
        s.append(s[-1] + sqrt(((R[i + 1] - R[i])**2).sum()))

    lines = []
    dEds0 = None
    for i in range(n):
        if i == 0:
            d = R[1] - R[0]
            ds = 0.5 * s[1]
        elif i == n - 1:
            d = R[-1] - R[-2]
            ds = 0.5 * (s[-1] - s[-2])
        else:
            d = R[i + 1] - R[i - 1]
            ds = 0.25 * (s[i + 1] - s[i - 1])

        d = d / sqrt((d**2).sum())
        dEds = -(F[i] * d).sum()
        x = np.linspace(s[i] - ds, s[i] + ds, 3)
        y = E[i] + dEds * (x - s[i])
        lines.append((x, y))

        if i > 0:
            s0 = s[i - 1]
            s1 = s[i]
            x = np.linspace(s0, s1, 20, endpoint=False)
            c = np.linalg.solve(np.array([(1, s0, s0**2, s0**3),
                                          (1, s1, s1**2, s1**3),
                                          (0, 1, 2 * s0, 3 * s0**2),
                                          (0, 1, 2 * s1, 3 * s1**2)]),
                                np.array([E[i - 1], E[i], dEds0, dEds]))
            y = c[0] + x * (c[1] + x * (c[2] + x * c[3]))
            Sfit[(i - 1) * 20:i * 20] = x
            Efit[(i - 1) * 20:i * 20] = y
        
        dEds0 = dEds

    Sfit[-1] = s[-1]
    Efit[-1] = E[-1]
    return s, E, Sfit, Efit, lines


class NEBtools:
    """Class to make many of the common tools for NEB analysis available to
    the user. Useful for scripting the output of many jobs. Initialize with
    list of images which make up a single band."""

    def __init__(self, images):
        self._images = images

    def get_barrier(self, fit=True, raw=False):
        """Returns the barrier estimate from the NEB, along with the
        Delta E of the elementary reaction. If fit=True, the barrier is
        estimated based on the interpolated fit to the images; if
        fit=False, the barrier is taken as the maximum-energy image
        without interpolation. Set raw=True to get the raw energy of the
        transition state instead of the forward barrier."""
        s, E, Sfit, Efit, lines = self.get_fit()
        dE = E[-1] - E[0]
        if fit:
            barrier = max(Efit)
        else:
            barrier = max(E)
        if raw:
            barrier += self._images[0].get_potential_energy()
        return barrier, dE

    def plot_band(self, ax=None):
        """Plots the NEB band on matplotlib axes object 'ax'. If ax=None
        returns a new figure object."""
        if not ax:
            from matplotlib import pyplot
            fig = pyplot.figure()
            ax = fig.add_subplot(111)
        else:
            fig = None
        s, E, Sfit, Efit, lines = self.get_fit()
        ax.plot(s, E, 'o')
        for x, y in lines:
            ax.plot(x, y, '-g')
        ax.plot(Sfit, Efit, 'k-')
        ax.set_xlabel('path [$\AA$]')
        ax.set_ylabel('energy [eV]')
        Ef = max(Efit) - E[0]
        Er = max(Efit) - E[-1]
        dE = E[-1] - E[0]
        ax.set_title('$E_\mathrm{f} \\approx$ %.3f eV; '
                     '$E_\mathrm{r} \\approx$ %.3f eV; '
                     '$\\Delta E$ = %.3f eV'
                     % (Ef, Er, dE))
        return fig

    def get_fmax(self):
        """Returns fmax, as used by optimizers with NEB."""
        neb = NEB(self._images)
        forces = neb.get_forces()
        return np.sqrt((forces**2).sum(axis=1).max())

    def get_fit(self):
        """Returns the parameters for fitting images to band."""
        images = self._images
        if not hasattr(images, 'repeat'):
            from ase.gui.images import Images
            images = Images(images)
        N = images.repeat.prod()
        natoms = images.natoms // N
        R = images.P[:, :natoms]
        E = images.E
        F = images.F[:, :natoms]
        s, E, Sfit, Efit, lines = fit0(E, F, R)
        return s, E, Sfit, Efit, lines


def interpolate(images):
    """Given a list of images, linearly interpolate the positions of the
    interior images."""
    pos1 = images[0].get_positions()
    pos2 = images[-1].get_positions()
    d = (pos2 - pos1) / (len(images) - 1.0)
    for i in range(1, len(images) - 1):
        images[i].set_positions(pos1 + i * d)
        # Parallel NEB with Jacapo needs this:
        try:
            images[i].get_calculator().set_atoms(images[i])
        except AttributeError:
            pass
