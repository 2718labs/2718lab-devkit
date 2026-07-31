"""Canonical Atlas package cutover contracts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_atlas_package_exposes_canonical_public_types_and_embedded_assets(
    tmp_path: Path,
) -> None:
    import devkit_atlas
    from devkit_atlas.receipts import (
        EVIDENCE_KEY_FILENAME,
        ReceiptRepository,
    )
    from devkit_atlas.routing import load_host_profiles
    from devkit_atlas.service import (
        AcceptedAtlasProjectionEvidence,
        AcceptedAtlasProjectionRequest,
        AtlasService,
    )

    package_root = Path(devkit_atlas.__file__).resolve().parent

    assert AtlasService.__name__ == "AtlasService"
    assert AcceptedAtlasProjectionRequest.__name__ == "AcceptedAtlasProjectionRequest"
    assert AcceptedAtlasProjectionEvidence.__name__ == "AcceptedAtlasProjectionEvidence"
    assert devkit_atlas.ASSET_ROOT == package_root / "assets"
    assert (package_root / "assets" / "host-profiles.json").is_file()
    assert (package_root / "assets" / "recipes").is_dir()
    assert "hosts" in load_host_profiles()
    assert EVIDENCE_KEY_FILENAME == "atlas-evidence.key"
    assert (
        ReceiptRepository(tmp_path).receipt_root
        == tmp_path / "atlas-receipts" / "sha256"
    )
    assert importlib.util.find_spec("code_atlas") is None
