import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def serialize_axtree(dom_state, remove_empty_nodes: bool = False) -> str:
    """Serialize a browser-use ``SerializedDOMState`` to its LLM/axtree string.

    Compatibility shim: some browser-use versions' ``llm_representation()`` do not accept
    ``remove_empty_nodes``. Keeping this tolerance on the optexity side means our
    browser-use fork stays a clean superset of upstream (no signature changes to existing
    upstream methods)."""
    try:
        return dom_state.llm_representation(remove_empty_nodes=remove_empty_nodes)
    except TypeError:
        return dom_state.llm_representation()


def _download_extension(url: str, output_path: Path) -> None:
    """Download extension .crx file."""
    import urllib.request

    try:
        logger.info(f"Downloading from: {url}")
        with urllib.request.urlopen(url) as response:
            content = response.read()
            logger.info(f"Downloaded {len(content)} bytes")
            with open(output_path, "wb") as f:
                f.write(content)
        logger.info(f"Saved to: {output_path}")
    except Exception as e:
        raise Exception(f"Failed to download extension: {e}")


def _extract_extension(crx_path: Path, extract_dir: Path) -> None:
    """Extract .crx file to directory."""
    import os
    import shutil
    import zipfile

    # Remove existing directory
    if extract_dir.exists():
        shutil.rmtree(extract_dir)

    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        # CRX files are ZIP files with a header, try to extract as ZIP
        with zipfile.ZipFile(crx_path, "r") as zip_ref:
            zip_ref.extractall(extract_dir)

        # Verify manifest exists
        if not (extract_dir / "manifest.json").exists():
            raise Exception("No manifest.json found in extension")

        logger.info("✅ Extracted as regular ZIP file")

    except zipfile.BadZipFile:
        logger.info("📦 Processing CRX header...")
        # CRX files have a header before the ZIP data
        with open(crx_path, "rb") as f:
            # Read CRX header to find ZIP start
            magic = f.read(4)
            if magic != b"Cr24":
                raise Exception(f"Invalid CRX file format. Magic: {magic}")

            version = int.from_bytes(f.read(4), "little")
            logger.info(f"CRX version: {version}")

            if version == 2:
                pubkey_len = int.from_bytes(f.read(4), "little")
                sig_len = int.from_bytes(f.read(4), "little")
                f.seek(16 + pubkey_len + sig_len)
            elif version == 3:
                header_len = int.from_bytes(f.read(4), "little")
                f.seek(12 + header_len)
            else:
                raise Exception(f"Unsupported CRX version: {version}")

            # Extract ZIP data
            zip_data = f.read()
            logger.info(f"ZIP data size: {len(zip_data)} bytes")

        # Write ZIP data to temp file and extract
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp_zip:
            temp_zip.write(zip_data)
            temp_zip.flush()

            with zipfile.ZipFile(temp_zip.name, "r") as zip_ref:
                zip_ref.extractall(extract_dir)

            os.unlink(temp_zip.name)

    # Remove 'key' from manifest if present (can cause issues)
    manifest_path = extract_dir / "manifest.json"
    if manifest_path.exists():
        data = json.loads(manifest_path.read_text())
        logger.info(f"Manifest version: {data.get('manifest_version')}")
        logger.info(f"Extension name: {data.get('name')}")

        if "key" in data:
            logger.info("Removing 'key' field from manifest")
            del data["key"]
            manifest_path.write_text(json.dumps(data, indent=2))
    else:
        raise Exception("manifest.json not found after extraction")
