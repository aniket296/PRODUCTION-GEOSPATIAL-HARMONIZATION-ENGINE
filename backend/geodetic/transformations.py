"""Geodetic Datum Transformation Subsystem.

Implements rigorous 3D Cartesian conversions, 7-parameter Helmert (Bursa-Wolf and Position Vector),
Molodensky-Badekas 10-parameter models, and 2D bilinear grid shift interpolations
for Indian cadastral datums (Everest 1830 / Kalianpur 1975) to WGS 84.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import enum
import math
from typing import Any, Dict, List, Optional, Tuple

from backend.geodetic.registry import (
    EllipsoidDefinition,
    EVEREST_1830_ELLIPSOID,
    GRS80_ELLIPSOID,
    WGS84_ELLIPSOID,
)


class TransformationError(Exception):
    """Base exception for datum transformation errors."""
    pass


class GridOutOfBoundsError(TransformationError):
    """Raised when coordinate falls outside the shift grid coverage domain."""
    pass


class HelmertConvention(str, enum.Enum):
    """Sign convention for 7-parameter Helmert rotation angles."""

    POSITION_VECTOR = "POSITION_VECTOR"  # EPSG 9606 (standard international / IERS)
    COORDINATE_FRAME = "COORDINATE_FRAME"  # EPSG 9607 (Bursa-Wolf with reversed rotation signs)


@dataclass(frozen=True)
class HelmertParameters:
    """Parameters for 7-parameter or 10-parameter Helmert datum transformation.

    Translations in metres, rotations in arcseconds, scale difference in parts per million (ppm).
    """

    dx: float  # X translation (metres)
    dy: float  # Y translation (metres)
    dz: float  # Z translation (metres)
    rx: float = 0.0  # X rotation (arcseconds)
    ry: float = 0.0  # Y rotation (arcseconds)
    rz: float = 0.0  # Z rotation (arcseconds)
    scale_ppm: float = 0.0  # Scale difference (ppm)
    convention: HelmertConvention = HelmertConvention.POSITION_VECTOR
    x0: float = 0.0  # Centroid X for Molodensky-Badekas (metres)
    y0: float = 0.0  # Centroid Y for Molodensky-Badekas (metres)
    z0: float = 0.0  # Centroid Z for Molodensky-Badekas (metres)
    name: str = "Custom Helmert Parameter Set"

    @property
    def is_molodensky_badekas(self) -> bool:
        """True if non-zero centroid coordinates are specified."""
        return self.x0 != 0.0 or self.y0 != 0.0 or self.z0 != 0.0

    def to_inverse(self) -> "HelmertParameters":
        """Compute the mathematical inverse parameter set."""
        rev_convention = (
            HelmertConvention.COORDINATE_FRAME
            if self.convention == HelmertConvention.COORDINATE_FRAME
            else HelmertConvention.POSITION_VECTOR
        )
        return HelmertParameters(
            dx=-self.dx,
            dy=-self.dy,
            dz=-self.dz,
            rx=-self.rx,
            ry=-self.ry,
            rz=-self.rz,
            scale_ppm=-self.scale_ppm,
            convention=rev_convention,
            x0=self.x0,
            y0=self.y0,
            z0=self.z0,
            name=f"Inverse of {self.name}",
        )


# Survey of India Official & Standard Kalianpur 1975 to WGS 84 Transformation Parameters
KALIANPUR_1975_TO_WGS84_NATIONAL = HelmertParameters(
    name="Kalianpur 1975 to WGS 84 (National Mean - EPSG:1150)",
    dx=283.7,
    dy=735.9,
    dz=261.1,
    rx=0.0,
    ry=0.0,
    rz=0.0,
    scale_ppm=0.0,
    convention=HelmertConvention.POSITION_VECTOR,
)

EVEREST_1830_TO_WGS84_SOI_7P = HelmertParameters(
    name="Everest 1830 to WGS 84 (Survey of India 7-Parameter)",
    dx=295.0,
    dy=736.0,
    dz=257.0,
    rx=0.0,
    ry=0.0,
    rz=4.15,
    scale_ppm=-1.2,
    convention=HelmertConvention.COORDINATE_FRAME,
)

KALIANPUR_1975_ZONE_IIA_REGIONAL = HelmertParameters(
    name="Kalianpur 1975 Zone IIa (North-West / Central India) to WGS 84",
    dx=282.6,
    dy=734.2,
    dz=259.8,
    rx=0.12,
    ry=-0.35,
    rz=0.48,
    scale_ppm=0.5,
    convention=HelmertConvention.POSITION_VECTOR,
)

KALIANPUR_ZONE_I_TO_WGS84 = HelmertParameters(
    name="Kalianpur 1975 Zone I (North India) to WGS 84",
    dx=284.0,
    dy=736.5,
    dz=262.0,
    rx=0.08,
    ry=-0.28,
    rz=0.35,
    scale_ppm=0.3,
    convention=HelmertConvention.POSITION_VECTOR,
)

KALIANPUR_ZONE_IIA_TO_WGS84 = KALIANPUR_1975_ZONE_IIA_REGIONAL

KALIANPUR_ZONE_IIB_TO_WGS84 = HelmertParameters(
    name="Kalianpur 1975 Zone IIb (North-East / East India) to WGS 84",
    dx=281.5,
    dy=733.0,
    dz=258.5,
    rx=0.18,
    ry=-0.40,
    rz=0.55,
    scale_ppm=0.6,
    convention=HelmertConvention.POSITION_VECTOR,
)

KALIANPUR_ZONE_IIIA_TO_WGS84 = HelmertParameters(
    name="Kalianpur 1975 Zone IIIa (Peninsular West) to WGS 84",
    dx=285.2,
    dy=737.1,
    dz=263.4,
    rx=0.05,
    ry=-0.22,
    rz=0.30,
    scale_ppm=0.2,
    convention=HelmertConvention.POSITION_VECTOR,
)

KALIANPUR_ZONE_IIIB_TO_WGS84 = HelmertParameters(
    name="Kalianpur 1975 Zone IIIb (Peninsular East) to WGS 84",
    dx=283.0,
    dy=735.0,
    dz=260.5,
    rx=0.10,
    ry=-0.30,
    rz=0.42,
    scale_ppm=0.4,
    convention=HelmertConvention.POSITION_VECTOR,
)

KALIANPUR_ZONE_IVA_TO_WGS84 = HelmertParameters(
    name="Kalianpur 1975 Zone IVa (Deep South) to WGS 84",
    dx=286.0,
    dy=738.5,
    dz=264.8,
    rx=0.02,
    ry=-0.15,
    rz=0.20,
    scale_ppm=0.1,
    convention=HelmertConvention.POSITION_VECTOR,
)

SOI_REGIONAL_HELMERT_PRESETS: Dict[int, HelmertParameters] = {
    24378: KALIANPUR_ZONE_I_TO_WGS84,
    24379: KALIANPUR_ZONE_IIA_TO_WGS84,
    24380: KALIANPUR_ZONE_IIB_TO_WGS84,
    24381: KALIANPUR_ZONE_IIIA_TO_WGS84,
    24382: KALIANPUR_ZONE_IIIB_TO_WGS84,
    24383: KALIANPUR_ZONE_IVA_TO_WGS84,
}


class EllipsoidConversion:
    """Rigorous transformations between geodetic (lon, lat, height) and 3D Cartesian coordinates."""

    @staticmethod
    def geodetic_to_cartesian(
        longitude: float,
        latitude: float,
        height: float,
        ellipsoid: EllipsoidDefinition,
    ) -> Tuple[float, float, float]:
        """Convert geodetic coordinates (lon, lat in deg, height in metres) to 3D Cartesian (X, Y, Z)."""
        lon_rad = math.radians(longitude)
        lat_rad = math.radians(latitude)

        a = ellipsoid.semi_major_axis
        b = ellipsoid.semi_minor_axis
        e2 = (a * a - b * b) / (a * a)

        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        sin_lon = math.sin(lon_rad)
        cos_lon = math.cos(lon_rad)

        # Radius of curvature in the prime vertical
        N = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)

        x = (N + height) * cos_lat * cos_lon
        y = (N + height) * cos_lat * sin_lon
        z = (N * (1.0 - e2) + height) * sin_lat

        return x, y, z

    @staticmethod
    def cartesian_to_geodetic(
        x: float,
        y: float,
        z: float,
        ellipsoid: EllipsoidDefinition,
    ) -> Tuple[float, float, float]:
        """Convert 3D Cartesian (X, Y, Z) to geodetic (lon, lat in deg, height in metres).

        Uses Bowring's closed vector formula with sub-millimetre precision globally.
        """
        a = ellipsoid.semi_major_axis
        b = ellipsoid.semi_minor_axis
        e2 = (a * a - b * b) / (a * a)
        e_prime2 = (a * a - b * b) / (b * b)

        p = math.hypot(x, y)

        if p < 1e-10:
            # Point is on or near the polar axis
            lat = 90.0 if z > 0 else -90.0
            lon = 0.0
            h = abs(z) - b
            return lon, lat, h

        lon_rad = math.atan2(y, x)

        # Bowring auxiliary angle theta
        theta = math.atan2(z * a, p * b)
        sin_theta = math.sin(theta)
        cos_theta = math.cos(theta)

        lat_rad = math.atan2(
            z + e_prime2 * b * (sin_theta**3),
            p - e2 * a * (cos_theta**3),
        )

        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        N = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)

        height = (p / cos_lat) - N

        lon_deg = math.degrees(lon_rad)
        lat_deg = math.degrees(lat_rad)

        return lon_deg, lat_deg, height


class HelmertTransform:
    """Execution engine for 7-parameter and 10-parameter 3D Helmert transformations."""

    ARCSEC_TO_RAD = math.pi / (180.0 * 3600.0)

    @classmethod
    def transform_cartesian(
        cls,
        point: Tuple[float, float, float],
        params: HelmertParameters,
        inverse: bool = False,
    ) -> Tuple[float, float, float]:
        """Apply 7-parameter Helmert or Molodensky-Badekas transformation in 3D Cartesian space."""
        x, y, z = point

        rx = params.rx * cls.ARCSEC_TO_RAD
        ry = params.ry * cls.ARCSEC_TO_RAD
        rz = params.rz * cls.ARCSEC_TO_RAD

        # Sign of rotation:
        # Position Vector (EPSG 9606): sgn = -1.0
        # Coordinate Frame / Bursa-Wolf (EPSG 9607): sgn = +1.0
        sgn = 1.0 if params.convention == HelmertConvention.COORDINATE_FRAME else -1.0

        r01 = sgn * rz
        r02 = -sgn * ry
        r10 = -sgn * rz
        r12 = sgn * rx
        r20 = sgn * ry
        r21 = -sgn * rx

        scale = 1.0 + (params.scale_ppm * 1e-6)

        if not inverse:
            # Forward transformation:
            # X' = X0 + dX + scale * ( (I + R) (X - X0) )
            xc = x - params.x0
            yc = y - params.y0
            zc = z - params.z0

            xr = xc + r01 * yc + r02 * zc
            yr = r10 * xc + yc + r12 * zc
            zr = r20 * xc + r21 * yc + zc

            x_out = params.x0 + params.dx + scale * xr
            y_out = params.y0 + params.dy + scale * yr
            z_out = params.z0 + params.dz + scale * zr

            return x_out, y_out, z_out
        else:
            # Exact mathematical inverse:
            # Solve (I + R) Xc = (X_out - X0 - dX) / scale
            vx = (x - params.x0 - params.dx) / scale
            vy = (y - params.y0 - params.dy) / scale
            vz = (z - params.z0 - params.dz) / scale

            # Determinant of (I + R) where R is skew-symmetric
            det_r = 1.0 + r01 * r01 + r02 * r02 + r12 * r12

            # Exact analytical inverse matrix of (I + R)
            inv_00 = 1.0 + r12 * r12
            inv_01 = -r01 - r12 * r02
            inv_02 = r01 * r12 - r02

            inv_10 = r01 - r12 * r02
            inv_11 = 1.0 + r02 * r02
            inv_12 = -r12 - r01 * r02

            inv_20 = r01 * r12 + r02
            inv_21 = r12 - r01 * r02
            inv_22 = 1.0 + r01 * r01

            xc = (inv_00 * vx + inv_01 * vy + inv_02 * vz) / det_r
            yc = (inv_10 * vx + inv_11 * vy + inv_12 * vz) / det_r
            zc = (inv_20 * vx + inv_21 * vy + inv_22 * vz) / det_r

            return params.x0 + xc, params.y0 + yc, params.z0 + zc

    @classmethod
    def transform_geodetic(
        cls,
        longitude: float,
        latitude: float,
        height: float,
        source_ellipsoid: EllipsoidDefinition,
        target_ellipsoid: EllipsoidDefinition,
        params: HelmertParameters,
        inverse: bool = False,
    ) -> Tuple[float, float, float]:
        """Full end-to-end geodetic datum transformation pipeline:

        (lon, lat, h) -> (X, Y, Z)_source -> (X, Y, Z)_target -> (lon, lat, h)_target.
        """
        # 1. Source geodetic to Cartesian
        xs, ys, zs = EllipsoidConversion.geodetic_to_cartesian(
            longitude, latitude, height, source_ellipsoid
        )

        # 2. Helmert transformation in Cartesian space
        xt, yt, zt = cls.transform_cartesian((xs, ys, zs), params, inverse=inverse)

        # 3. Target Cartesian to geodetic
        lon_t, lat_t, h_t = EllipsoidConversion.cartesian_to_geodetic(
            xt, yt, zt, target_ellipsoid
        )

        return lon_t, lat_t, h_t


@dataclass
class GridShiftNode:
    """Single node in a 2D distortion grid."""

    lon: float
    lat: float
    d_lon_sec: float  # Longitude shift in arcseconds
    d_lat_sec: float  # Latitude shift in arcseconds


@dataclass
class GridShiftTable:
    """Regular 2D grid of shift values for NTv2 or cadastral local residual distortion modeling."""

    name: str
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    step_lon: float  # Grid spacing in degrees
    step_lat: float  # Grid spacing in degrees
    # Matrix of (d_lon_sec, d_lat_sec) indexed by [row_lat][col_lon]
    shifts: List[List[Tuple[float, float]]]

    @property
    def num_rows(self) -> int:
        return len(self.shifts)

    @property
    def num_cols(self) -> int:
        return len(self.shifts[0]) if self.shifts else 0

    def contains(self, lon: float, lat: float) -> bool:
        """Check if point lies within grid domain."""
        return (
            self.min_lon <= lon <= self.max_lon
            and self.min_lat <= lat <= self.max_lat
        )

    def interpolate_shift(self, lon: float, lat: float) -> Tuple[float, float]:
        """Bilinearly interpolate shift (d_lon, d_lat in arcseconds) at the given coordinate.

        Raises:
            GridOutOfBoundsError: If point is outside grid boundaries.
        """
        if not self.contains(lon, lat):
            raise GridOutOfBoundsError(
                f"Point ({lon}, {lat}) is outside grid shift domain "
                f"[{self.min_lon}, {self.min_lat}, {self.max_lon}, {self.max_lat}]"
            )

        col_f = (lon - self.min_lon) / self.step_lon
        row_f = (lat - self.min_lat) / self.step_lat

        col0 = int(math.floor(col_f))
        row0 = int(math.floor(row_f))

        # Clamp boundary indices
        col0 = max(0, min(self.num_cols - 2, col0))
        row0 = max(0, min(self.num_rows - 2, row0))

        col1 = col0 + 1
        row1 = row0 + 1

        u = col_f - col0
        v = row_f - row0

        # Four corner shifts: s00, s10, s01, s11
        s00 = self.shifts[row0][col0]
        s10 = self.shifts[row0][col1]
        s01 = self.shifts[row1][col0]
        s11 = self.shifts[row1][col1]

        # Bilinear interpolation
        d_lon = (
            (1.0 - u) * (1.0 - v) * s00[0]
            + u * (1.0 - v) * s10[0]
            + (1.0 - u) * v * s01[0]
            + u * v * s11[0]
        )
        d_lat = (
            (1.0 - u) * (1.0 - v) * s00[1]
            + u * (1.0 - v) * s10[1]
            + (1.0 - u) * v * s01[1]
            + u * v * s11[1]
        )

        return d_lon, d_lat

    def apply_shift(self, lon: float, lat: float, inverse: bool = False) -> Tuple[float, float]:
        """Apply interpolated 2D shift to geographic coordinates.

        Shifts are added in forward mode, subtracted in inverse mode.
        """
        d_lon_sec, d_lat_sec = self.interpolate_shift(lon, lat)
        d_lon_deg = d_lon_sec / 3600.0
        d_lat_deg = d_lat_sec / 3600.0

        if inverse:
            return lon - d_lon_deg, lat - d_lat_deg
        return lon + d_lon_deg, lat + d_lat_deg


class DatumPipeline:
    """Configurable geodetic datum harmonization pipeline connecting Helmert and grid shift models."""

    def __init__(
        self,
        name: str,
        source_ellipsoid: EllipsoidDefinition,
        target_ellipsoid: EllipsoidDefinition,
        helmert_params: Optional[HelmertParameters] = None,
        grid_shift_table: Optional[GridShiftTable] = None,
    ) -> None:
        self.name = name
        self.source_ellipsoid = source_ellipsoid
        self.target_ellipsoid = target_ellipsoid
        self.helmert_params = helmert_params
        self.grid_shift_table = grid_shift_table

    def transform(
        self,
        longitude: float,
        latitude: float,
        height: float = 0.0,
        inverse: bool = False,
    ) -> Tuple[float, float, float]:
        """Execute datum transformation pipeline in forward or inverse direction."""
        curr_lon = longitude
        curr_lat = latitude
        curr_h = height

        if not inverse:
            # Forward: 1. Helmert -> 2. Grid Shift
            if self.helmert_params:
                curr_lon, curr_lat, curr_h = HelmertTransform.transform_geodetic(
                    curr_lon,
                    curr_lat,
                    curr_h,
                    self.source_ellipsoid,
                    self.target_ellipsoid,
                    self.helmert_params,
                    inverse=False,
                )
            if self.grid_shift_table:
                curr_lon, curr_lat = self.grid_shift_table.apply_shift(
                    curr_lon, curr_lat, inverse=False
                )
        else:
            # Inverse: 1. Inverse Grid Shift -> 2. Inverse Helmert
            if self.grid_shift_table:
                curr_lon, curr_lat = self.grid_shift_table.apply_shift(
                    curr_lon, curr_lat, inverse=True
                )
            if self.helmert_params:
                curr_lon, curr_lat, curr_h = HelmertTransform.transform_geodetic(
                    curr_lon,
                    curr_lat,
                    curr_h,
                    self.target_ellipsoid,  # Swapped for inverse
                    self.source_ellipsoid,  # Swapped for inverse
                    self.helmert_params,
                    inverse=True,
                )

        return curr_lon, curr_lat, curr_h


@dataclass
class HelmertFitResult:
    """Statistical estimation result from least-squares 7-parameter Helmert fitting."""

    parameters: HelmertParameters
    rmse_3d: float
    rmse_x: float
    rmse_y: float
    rmse_z: float
    residuals: List[Tuple[float, float, float]]  # (vx, vy, vz) per point in metres
    num_points: int
    degrees_of_freedom: int
    sigma_0: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parameters": {
                "dx": round(self.parameters.dx, 4),
                "dy": round(self.parameters.dy, 4),
                "dz": round(self.parameters.dz, 4),
                "rx": round(self.parameters.rx, 4),
                "ry": round(self.parameters.ry, 4),
                "rz": round(self.parameters.rz, 4),
                "scale_ppm": round(self.parameters.scale_ppm, 4),
                "convention": self.parameters.convention.value,
                "x0": round(self.parameters.x0, 4),
                "y0": round(self.parameters.y0, 4),
                "z0": round(self.parameters.z0, 4),
            },
            "rmse_3d": round(self.rmse_3d, 4),
            "rmse_x": round(self.rmse_x, 4),
            "rmse_y": round(self.rmse_y, 4),
            "rmse_z": round(self.rmse_z, 4),
            "num_points": self.num_points,
            "degrees_of_freedom": self.degrees_of_freedom,
            "sigma_0": round(self.sigma_0, 6),
            "residuals": [
                [round(v[0], 4), round(v[1], 4), round(v[2], 4)]
                for v in self.residuals
            ],
        }


def _solve_linear_system(A: List[List[float]], b: List[float]) -> List[float]:
    """Solve A x = b using Gaussian elimination with partial pivoting."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]

    for i in range(n):
        max_row = i
        max_val = abs(M[i][i])
        for r in range(i + 1, n):
            if abs(M[r][i]) > max_val:
                max_val = abs(M[r][i])
                max_row = r
        if max_val < 1e-12:
            raise TransformationError("Degenerate or ill-conditioned normal matrix in Helmert parameter adjustment.")

        if max_row != i:
            M[i], M[max_row] = M[max_row], M[i]

        pivot = M[i][i]
        for col in range(i, n + 1):
            M[i][col] /= pivot

        for r in range(n):
            if r != i:
                factor = M[r][i]
                if abs(factor) > 1e-15:
                    for col in range(i, n + 1):
                        M[r][col] -= factor * M[i][col]

    return [M[i][n] for i in range(n)]


class HelmertFitter:
    """Least-squares estimator for 7-parameter Helmert or Molodensky-Badekas models from Ground Control Points."""

    @classmethod
    def fit(
        cls,
        source_points: List[Tuple[float, float, float]],
        target_points: List[Tuple[float, float, float]],
        convention: HelmertConvention = HelmertConvention.POSITION_VECTOR,
        is_molodensky_badekas: bool = False,
    ) -> HelmertFitResult:
        n_pts = len(source_points)
        if n_pts != len(target_points):
            raise TransformationError(f"Point count mismatch: {n_pts} source points vs {len(target_points)} target points.")
        if n_pts < 3:
            raise TransformationError(f"Minimum 3 3D GCP point pairs required for 7-parameter Helmert fitting, got {n_pts}.")

        x0 = sum(p[0] for p in source_points) / n_pts
        y0 = sum(p[1] for p in source_points) / n_pts
        z0 = sum(p[2] for p in source_points) / n_pts

        eval_x0 = x0 if is_molodensky_badekas else 0.0
        eval_y0 = y0 if is_molodensky_badekas else 0.0
        eval_z0 = z0 if is_molodensky_badekas else 0.0

        # Sign of rotation: Position Vector (sgn = -1.0) vs Coordinate Frame (sgn = +1.0)
        sgn = 1.0 if convention == HelmertConvention.COORDINATE_FRAME else -1.0

        A: List[List[float]] = []
        L: List[float] = []

        for (xs, ys, zs), (xt, yt, zt) in zip(source_points, target_points):
            xi = xs - eval_x0
            yi = ys - eval_y0
            zi = zs - eval_z0

            # Row for X: dx - sgn*zi*ry + sgn*yi*rz + xi*ds = Xt - Xs
            A.append([1.0, 0.0, 0.0, 0.0, -sgn * zi, sgn * yi, xi])
            L.append(xt - xs)

            # Row for Y: dy + sgn*zi*rx - sgn*xi*rz + yi*ds = Yt - Ys
            A.append([0.0, 1.0, 0.0, sgn * zi, 0.0, -sgn * xi, yi])
            L.append(yt - ys)

            # Row for Z: dz - sgn*yi*rx + sgn*xi*ry + zi*ds = Zt - Zs
            A.append([0.0, 0.0, 1.0, -sgn * yi, sgn * xi, 0.0, zi])
            L.append(zt - zs)

        m_obs = len(A)
        N = [[0.0] * 7 for _ in range(7)]
        U = [0.0] * 7

        for row_idx in range(m_obs):
            row = A[row_idx]
            l_val = L[row_idx]
            for j in range(7):
                U[j] += row[j] * l_val
                for k in range(j, 7):
                    val = row[j] * row[k]
                    N[j][k] += val
                    if j != k:
                        N[k][j] += val

        sol = _solve_linear_system(N, U)

        dx = sol[0]
        dy = sol[1]
        dz = sol[2]
        rx_rad = sol[3]
        ry_rad = sol[4]
        rz_rad = sol[5]
        scale_unitless = sol[6]

        rx_arcsec = rx_rad * (180.0 * 3600.0 / math.pi)
        ry_arcsec = ry_rad * (180.0 * 3600.0 / math.pi)
        rz_arcsec = rz_rad * (180.0 * 3600.0 / math.pi)
        scale_ppm = scale_unitless * 1e6

        fitted_params = HelmertParameters(
            name="Least-Squares Estimated Helmert Parameters",
            dx=dx,
            dy=dy,
            dz=dz,
            rx=rx_arcsec,
            ry=ry_arcsec,
            rz=rz_arcsec,
            scale_ppm=scale_ppm,
            convention=convention,
            x0=eval_x0,
            y0=eval_y0,
            z0=eval_z0,
        )

        residuals: List[Tuple[float, float, float]] = []
        sum_sq_x = 0.0
        sum_sq_y = 0.0
        sum_sq_z = 0.0

        for sp, tp in zip(source_points, target_points):
            trans_p = HelmertTransform.transform_cartesian(sp, fitted_params)
            vx = tp[0] - trans_p[0]
            vy = tp[1] - trans_p[1]
            vz = tp[2] - trans_p[2]
            residuals.append((vx, vy, vz))
            sum_sq_x += vx * vx
            sum_sq_y += vy * vy
            sum_sq_z += vz * vz

        sum_sq_3d = sum_sq_x + sum_sq_y + sum_sq_z
        rmse_3d = math.sqrt(sum_sq_3d / n_pts)
        rmse_x = math.sqrt(sum_sq_x / n_pts)
        rmse_y = math.sqrt(sum_sq_y / n_pts)
        rmse_z = math.sqrt(sum_sq_z / n_pts)

        dof = max(1, 3 * n_pts - 7)
        sigma_0 = math.sqrt(sum_sq_3d / dof)

        return HelmertFitResult(
            parameters=fitted_params,
            rmse_3d=rmse_3d,
            rmse_x=rmse_x,
            rmse_y=rmse_y,
            rmse_z=rmse_z,
            residuals=residuals,
            num_points=n_pts,
            degrees_of_freedom=dof,
            sigma_0=sigma_0,
        )


def get_transformation_pipeline(
    source_srid: str,
    target_srid: str,
) -> Optional[DatumPipeline]:
    """Retrieve or construct the optimal geodetic datum harmonization pipeline between two SRIDs."""
    from backend.geodetic.registry import crs_registry

    src_rec = crs_registry.get(source_srid, strict=False)
    tgt_rec = crs_registry.get(target_srid, strict=False)

    if not src_rec or not tgt_rec:
        return None

    # If same ellipsoid / datum, pipeline is identity
    if src_rec.datum_name == tgt_rec.datum_name and src_rec.ellipsoid.name == tgt_rec.ellipsoid.name:
        return DatumPipeline(
            name=f"Identity pipeline ({source_srid} -> {target_srid})",
            source_ellipsoid=src_rec.ellipsoid,
            target_ellipsoid=tgt_rec.ellipsoid,
            helmert_params=None,
        )

    # Kalianpur 1975 to WGS 84
    is_src_kalianpur = "Kalianpur" in src_rec.datum_name or "Everest" in src_rec.datum_name
    is_tgt_wgs = "WGS" in tgt_rec.datum_name or "World Geodetic System" in tgt_rec.datum_name or "WGS" in tgt_rec.name
    if is_src_kalianpur and is_tgt_wgs:
        helmert_p = SOI_REGIONAL_HELMERT_PRESETS.get(src_rec.epsg_code, KALIANPUR_1975_TO_WGS84_NATIONAL)
        return DatumPipeline(
            name=f"{src_rec.name} to {tgt_rec.name}",
            source_ellipsoid=src_rec.ellipsoid,
            target_ellipsoid=tgt_rec.ellipsoid,
            helmert_params=helmert_p,
        )

    # WGS 84 to Kalianpur 1975 (Inverse)
    is_src_wgs = "WGS" in src_rec.datum_name or "World Geodetic System" in src_rec.datum_name or "WGS" in src_rec.name
    is_tgt_kalianpur = "Kalianpur" in tgt_rec.datum_name or "Everest" in tgt_rec.datum_name
    if is_src_wgs and is_tgt_kalianpur:
        helmert_p = SOI_REGIONAL_HELMERT_PRESETS.get(tgt_rec.epsg_code, KALIANPUR_1975_TO_WGS84_NATIONAL)
        return DatumPipeline(
            name=f"{src_rec.name} to {tgt_rec.name} (Inverse)",
            source_ellipsoid=src_rec.ellipsoid,
            target_ellipsoid=tgt_rec.ellipsoid,
            helmert_params=helmert_p.to_inverse(),
        )

    # Generic fallback using National Mean
    return DatumPipeline(
        name=f"Generic Datum Harmonization ({src_rec.name} -> {tgt_rec.name})",
        source_ellipsoid=src_rec.ellipsoid,
        target_ellipsoid=tgt_rec.ellipsoid,
        helmert_params=KALIANPUR_1975_TO_WGS84_NATIONAL,
    )


def transform_coordinates_batch(
    coords: List[Tuple[float, ...]],
    pipeline: DatumPipeline,
    inverse: bool = False,
) -> List[Tuple[float, ...]]:
    """Batch transform a list of 2D (lon, lat) or 3D (lon, lat, h) coordinate tuples."""
    results: List[Tuple[float, ...]] = []
    for pt in coords:
        if len(pt) == 2:
            lon, lat = pt
            t_lon, t_lat, _ = pipeline.transform(lon, lat, 0.0, inverse=inverse)
            results.append((t_lon, t_lat))
        elif len(pt) >= 3:
            lon, lat, h = pt[0], pt[1], pt[2]
            t_lon, t_lat, t_h = pipeline.transform(lon, lat, h, inverse=inverse)
            results.append((t_lon, t_lat, t_h))
        else:
            raise TransformationError(f"Coordinate tuple must have at least 2 elements, got {pt}")
    return results


def transform_geojson_geometry(
    geometry: Dict[str, Any],
    pipeline: DatumPipeline,
    inverse: bool = False,
) -> Dict[str, Any]:
    """Recursively transform coordinates of a GeoJSON geometry dictionary."""
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")

    def _transform_point(pt: List[float]) -> List[float]:
        h = pt[2] if len(pt) > 2 else 0.0
        t_lon, t_lat, t_h = pipeline.transform(pt[0], pt[1], h, inverse=inverse)
        return [t_lon, t_lat, t_h] if len(pt) > 2 else [t_lon, t_lat]

    def _transform_line(line: List[List[float]]) -> List[List[float]]:
        return [_transform_point(pt) for pt in line]

    def _transform_polygon(poly: List[List[List[float]]]) -> List[List[List[float]]]:
        return [_transform_line(ring) for ring in poly]

    if geom_type == "Point":
        new_coords = _transform_point(coords)
    elif geom_type in ("MultiPoint", "LineString"):
        new_coords = _transform_line(coords)
    elif geom_type in ("MultiLineString", "Polygon"):
        new_coords = _transform_polygon(coords)
    elif geom_type == "MultiPolygon":
        new_coords = [_transform_polygon(poly) for poly in coords]
    elif geom_type == "GeometryCollection":
        geometries = [
            transform_geojson_geometry(g, pipeline, inverse=inverse)
            for g in geometry.get("geometries", [])
        ]
        return {"type": "GeometryCollection", "geometries": geometries}
    else:
        raise TransformationError(f"Unsupported GeoJSON geometry type: '{geom_type}'")

    return {
        "type": geom_type,
        "coordinates": new_coords,
    }

