"""Build float64 calculation vectors with public bw_processing APIs.

The default iterator path in bw_processing 1.6 uses float32, including for
uncertainty parameters. Explicit arrays avoid this intermediate loss without
changing any Brightway globals or modifying other databases.
"""

import os
import tempfile
from pathlib import Path


def write_database_calculation_data(bd, database: str, data: dict) -> None:
    rows = {"technosphere_matrix": [], "biosphere_matrix": [], "inv_geomapping_matrix": []}
    keys = set(data) | {tuple(e["input"]) for d in data.values() for e in d.get("exchanges", [])}
    ids = {key: bd.get_node(database=key[0], code=key[1]).id for key in keys}
    for key, dataset in data.items():
        col = ids[key]
        rows["inv_geomapping_matrix"].append(
            {
                "row": col,
                "col": bd.geomapping[dataset.get("location") or bd.config.global_location],
                "amount": 1,
            }
        )
        for exchange in dataset["exchanges"]:
            matrix = (
                "biosphere_matrix" if exchange["type"] == "biosphere" else "technosphere_matrix"
            )
            rows[matrix].append(
                {
                    **exchange,
                    "row": ids[tuple(exchange["input"])],
                    "col": col,
                    "flip": exchange["type"] == "technosphere",
                }
            )
    store = bd.Database(database)
    _write(store.filepath_processed(), rows, dependencies=store.metadata.get("depends", []))


def write_method_calculation_data(bd, name: tuple) -> None:
    store = bd.Method(name)
    _write(
        store.filepath_processed(),
        {"characterization_matrix": [store.process_row(row) for row in store.load()]},
        global_index=bd.geomapping[bd.config.global_location],
        identifier=list(name),
    )


def _write(path, matrices, *, dependencies=None, **vector_metadata):
    import bw_processing as bwp
    import numpy as np
    from fsspec.implementations.zip import ZipFileSystem

    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".tidas-float64-", suffix=".zip", dir=path.parent
    )
    os.close(descriptor)
    try:
        dp = bwp.create_datapackage(
            fs=ZipFileSystem(temporary, mode="w"),
            sum_intra_duplicates=True,
            sum_inter_duplicates=False,
        )
        for matrix, rows in matrices.items():
            indices = np.array([(r["row"], r["col"]) for r in rows], dtype=bwp.INDICES_DTYPE)
            amounts = np.array([r["amount"] for r in rows], dtype=np.float64)
            # Same fields as stats_arrays, explicitly retaining float64 for all parameters.
            dtype = [
                ("uncertainty_type", np.uint8),
                ("loc", np.float64),
                ("scale", np.float64),
                ("shape", np.float64),
                ("minimum", np.float64),
                ("maximum", np.float64),
                ("negative", bool),
            ]
            distributions = np.zeros(len(rows), dtype=dtype)
            for i, row in enumerate(rows):
                kind = row.get("uncertainty type", 0)
                distributions[i]["uncertainty_type"] = kind
                for field in ("loc", "scale", "shape", "minimum", "maximum"):
                    distributions[i][field] = row.get(field, np.nan)
                if kind < 2:
                    distributions[i]["loc"] = row["amount"]
                distributions[i]["negative"] = row.get("negative", False)
            dp.add_persistent_vector(
                matrix=matrix,
                name=matrix,
                indices_array=indices,
                data_array=amounts,
                distributions_array=distributions,
                flip_array=np.array([r.get("flip", False) for r in rows], dtype=bool),
                **vector_metadata,
            )
        if dependencies is not None:
            dp.metadata["database_dependencies"] = dependencies
        dp.finalize_serialization()
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
