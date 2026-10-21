"""Regression guard for independently bundled Python certificate trust."""

import importlib.metadata
import ssl

import certifi
from packaging.version import Version


def test_runtime_certifi_includes_emudhra_distrust_release():
    assert Version(importlib.metadata.version("certifi")) >= Version("2026.10.19")


def test_runtime_bundle_loads_without_compromised_root():
    context = ssl.create_default_context(cafile=certifi.where())
    roots = context.get_ca_certs()
    assert roots, "A broken or empty trust store is not a security fix"
    common_names = {
        value
        for root in roots
        for rdn in root["subject"]
        for name, value in rdn
        if name == "commonName"
    }
    assert "emSign ECC Root CA - G3" not in common_names


def test_bundled_crypto_security_release_and_signing_roundtrip():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    assert Version(importlib.metadata.version("cryptography")) >= Version("50.0.2")
    private_key = Ed25519PrivateKey.generate()
    message = b"security-runtime-probe"
    signature = private_key.sign(message)
    private_key.public_key().verify(signature, message)
