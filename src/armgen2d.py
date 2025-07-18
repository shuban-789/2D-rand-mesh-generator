#!/usr/bin/python3
from mpi4py import MPI
from dolfinx.io import gmshio, XDMFFile
from scipy.stats import truncnorm
import gmsh
import random
import math
import json
import os

class MeshGenerator:
    def __init__(self, layout, size, circles, randomized_max_radius, distribution,
                 set_circle_radius, mesh_element_size, randomized_radius, min_fraction_inside=0.3):
        self.layout = layout
        self.layout_x = float(layout[0])
        self.layout_y = float(layout[1])
        self.size = size
        self.circles = circles
        self.randomized_max_radius = randomized_max_radius
        self.distribution = distribution
        self.set_circle_radius = set_circle_radius
        self.mesh_element_size = mesh_element_size
        self.randomized_radius = randomized_radius
        self.placed_circles = []
        self.min_fraction_inside = min_fraction_inside
        self.circle_area_sum = 0.0
        self.square_area_sum = self.layout_x * self.layout_y

    def check_circ_overlap(self, x1, y1, r1, x2, y2, r2) -> bool:
        d = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        return d < (r1 + r2)

    def is_enough_inside(self, cx, cy, radius):
        x_min = cx - radius
        x_max = cx + radius
        y_min = cy - radius
        y_max = cy + radius

        x_overlap = max(0, min(x_max, self.layout_x) - max(x_min, 0))
        y_overlap = max(0, min(y_max, self.layout_y) - max(y_min, 0))

        overlap_area = x_overlap * y_overlap
        circle_area = math.pi * radius * radius

        return overlap_area / circle_area >= self.min_fraction_inside

    def create_rect(self):
        rect = gmsh.model.occ.addRectangle(0, 0, 0, self.layout_x, self.layout_y)
        gmsh.model.occ.synchronize()
        rect_edges = gmsh.model.getBoundary([(2, rect)], oriented=True)
        return rect, rect_edges

    def add_circle(self, cx, cy, radius):
        return gmsh.model.occ.addDisk(cx, cy, 0, radius, radius)

    def truncated_gaussian(self, rmin, rmax, rmean, rstd):
        a, b = (rmin - rmean) / rstd, (rmax - rmean) / rstd
        return truncnorm.rvs(a, b, loc=rmean, scale=rstd)

    def generate(self, visualize=True, save_path=None):
        comm = MPI.COMM_WORLD
        rank = comm.rank

        gmsh.initialize()
        gmsh.model.add("Mesh Result")
        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", self.mesh_element_size)

        rect, rect_edges = self.create_rect()
        circle_tags = []

        placed_count = 0
        while placed_count < self.circles:
            valid_placement = False
            while not valid_placement:
                if self.randomized_radius:
                    if self.distribution == "uniform":
                        circle_radius = random.uniform(0.1, self.randomized_max_radius)
                    elif self.distribution == "gaussian":
                        circle_radius = self.truncated_gaussian(
                            rmin=0.1,
                            rmax=self.randomized_max_radius,
                            rmean=(self.randomized_max_radius + 0.1) / 2,
                            rstd=(self.randomized_max_radius - 0.1) / 4
                        )
                    else:
                        raise ValueError("Unsupported distribution type.")
                else:
                    circle_radius = self.set_circle_radius

                cx = random.uniform(-circle_radius, self.layout_x + circle_radius)
                cy = random.uniform(-circle_radius, self.layout_y + circle_radius)

                potential_positions = [(cx, cy)]

                if cx - circle_radius < 0:
                    potential_positions.append((cx + self.layout_x, cy))
                if cx + circle_radius > self.layout_x:
                    potential_positions.append((cx - self.layout_x, cy))
                if cy - circle_radius < 0:
                    potential_positions.append((cx, cy + self.layout_y))
                if cy + circle_radius > self.layout_y:
                    potential_positions.append((cx, cy - self.layout_y))

                if cx - circle_radius < 0 and cy - circle_radius < 0:
                    potential_positions.append((cx + self.layout_x, cy + self.layout_y))
                if cx + circle_radius > self.layout_x and cy - circle_radius < 0:
                    potential_positions.append((cx - self.layout_x, cy + self.layout_y))
                if cx - circle_radius < 0 and cy + circle_radius > self.layout_y:
                    potential_positions.append((cx + self.layout_x, cy - self.layout_y))
                if cx + circle_radius > self.layout_x and cy + circle_radius > self.layout_y:
                    potential_positions.append((cx - self.layout_x, cy - self.layout_y))

                valid_placement = all(
                    not self.check_circ_overlap(px, py, circle_radius, x, y, r)
                    for px, py in potential_positions
                    for x, y, r in self.placed_circles
                ) and self.is_enough_inside(cx, cy, circle_radius)

            placed_count += 1
            self.placed_circles.append((cx, cy, circle_radius))
            circle_tags.append(self.add_circle(cx, cy, circle_radius))

            for px, py in potential_positions[1:]:
                self.placed_circles.append((px, py, circle_radius))
                circle_tags.append(self.add_circle(px, py, circle_radius))

        gmsh.model.occ.synchronize()

        valid_circle_tags = []
        for tag in circle_tags:
            try:
                gmsh.model.occ.getCenterOfMass(2, tag)
                valid_circle_tags.append(tag)
            except:
                pass

        gmsh.model.occ.fragment([(2, rect)], [(2, tag) for tag in valid_circle_tags])
        gmsh.model.occ.synchronize()

        all_edges = gmsh.model.getEntities(dim=1)

        def midpoint(tag):
            x, y, _ = gmsh.model.occ.getCenterOfMass(1, tag)
            return x, y

        tol = 1e-6
        left, right, bottom, top = [], [], [], []

        for (dim, tag) in all_edges:
            x, y = midpoint(tag)
            if abs(x - 0) < tol:
                left.append(tag)
            elif abs(x - self.layout_x) < tol:
                right.append(tag)
            elif abs(y - 0) < tol:
                bottom.append(tag)
            elif abs(y - self.layout_y) < tol:
                top.append(tag)

        gmsh.model.addPhysicalGroup(1, bottom, tag=1)
        gmsh.model.setPhysicalName(1, 1, "Bottom")
        gmsh.model.addPhysicalGroup(1, right, tag=2)
        gmsh.model.setPhysicalName(1, 2, "Right")
        gmsh.model.addPhysicalGroup(1, top, tag=3)
        gmsh.model.setPhysicalName(1, 3, "Top")
        gmsh.model.addPhysicalGroup(1, left, tag=4)
        gmsh.model.setPhysicalName(1, 4, "Left")

        circle_surfaces = valid_circle_tags
        gmsh.model.addPhysicalGroup(2, circle_surfaces, tag=1)
        gmsh.model.setPhysicalName(2, 1, "Circles")

        all_surfaces = gmsh.model.getEntities(dim=2)
        background_surfaces = [s[1] for s in all_surfaces if s[1] not in circle_surfaces]
        gmsh.model.addPhysicalGroup(2, background_surfaces, tag=2)
        gmsh.model.setPhysicalName(2, 2, "Background")

        gmsh.model.mesh.generate(2)

        mesh, cell_tags, facet_tags, *rest = gmshio.model_to_mesh(gmsh.model, comm, 0, gdim=2)
        mesh.topology.create_entities(mesh.topology.dim - 1)
        mesh.topology.create_connectivity(mesh.topology.dim - 1, mesh.topology.dim)

        cell_tags.name = "cell_tags"
        facet_tags.name = "facet_tags"

        with XDMFFile(comm, save_path, "w") as xdmf:
            xdmf.write_mesh(mesh)
            xdmf.write_meshtags(cell_tags)
            xdmf.write_meshtags(facet_tags)

        self.circle_area_sum = sum(math.pi * r * r for _, _, r in self.placed_circles)
        distribution = (self.circle_area_sum / self.square_area_sum) * 100

        data = {
            "id": rank,
            "circles": self.circles,
            "distribution": distribution
        }

        save_dir = os.path.dirname(save_path)
        json_path = os.path.join(save_dir, "meshinfo.json")

        json.dump(data, open(json_path, "w"))

        if visualize:
            try:
                gmsh.fltk.run()
            except Exception as e:
                pass

        gmsh.finalize()