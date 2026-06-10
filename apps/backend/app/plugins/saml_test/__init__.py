"""plugins/saml_test — test-only stub SAML IdP.

Refuses to load outside `APP_MODE=test`. Issues itsdangerous-signed
assertions standing in for SAML XML — the orchestration (assertion →
session-satisfied) is what we want to exercise end-to-end; the XML+xmlsec1
round-trip belongs to `plugins/saml` and is left to integration testing
with a real IdP image.
"""

from app.plugins.saml_test.service import (
    bootstrap,
    sign_assertion,
    verify_assertion,
)

__all__ = ["bootstrap", "sign_assertion", "verify_assertion"]

bootstrap()
