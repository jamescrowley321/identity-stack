"""Registration tokens of Keycloak machine clients are sealed at rest (#698)."""

import pytest
from cryptography.fernet import Fernet

from app.repositories.registration_tokens import (
    KEYS_VARIABLE,
    RegistrationTokenCipher,
    RegistrationTokenKeysNotConfigured,
    RegistrationTokenNotSealed,
    RegistrationTokenUnreadable,
    registration_token_cipher,
)

TOKEN = "eyJhbGciOiJIUzUxMiJ9.registration-token.signature"
KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()


class TestCipher:
    def test_a_sealed_token_opens_to_itself_and_does_not_contain_it(self):
        cipher = RegistrationTokenCipher([KEY])

        sealed = cipher.seal(TOKEN)

        assert TOKEN not in sealed
        assert cipher.open(sealed) == TOKEN

    def test_a_token_sealed_under_another_key_is_unreadable(self):
        sealed = RegistrationTokenCipher([OTHER_KEY]).seal(TOKEN)

        with pytest.raises(RegistrationTokenUnreadable) as refused:
            RegistrationTokenCipher([KEY]).open(sealed)

        assert KEYS_VARIABLE in str(refused.value)
        assert not isinstance(refused.value, RegistrationTokenNotSealed)

    @pytest.mark.parametrize("stored", [TOKEN, "not base64 at all!", "AAAA"])
    def test_a_value_that_is_no_fernet_token_is_not_sealed_and_never_read(self, stored):
        with pytest.raises(RegistrationTokenNotSealed) as refused:
            RegistrationTokenCipher([KEY]).open(stored)

        assert stored not in str(refused.value)

    def test_the_first_key_seals_and_every_key_opens(self):
        old = RegistrationTokenCipher([OTHER_KEY]).seal(TOKEN)
        rotated = RegistrationTokenCipher([KEY, OTHER_KEY])

        new = rotated.seal(TOKEN)

        assert (rotated.open(old), RegistrationTokenCipher([KEY]).open(new)) == (TOKEN, TOKEN)

    def test_no_token_stays_empty(self):
        cipher = RegistrationTokenCipher([KEY])

        assert (cipher.seal(""), cipher.open("")) == ("", "")

    def test_its_repr_names_no_key(self):
        assert KEY not in repr(RegistrationTokenCipher([KEY]))


class TestSettings:
    def test_reads_every_listed_key(self, monkeypatch):
        sealed = RegistrationTokenCipher([OTHER_KEY]).seal(TOKEN)
        monkeypatch.setenv(KEYS_VARIABLE, f" {KEY} , {OTHER_KEY} ,")

        assert registration_token_cipher().open(sealed) == TOKEN

    @pytest.mark.parametrize("unset_value", [None, "", " , "])
    def test_no_key_is_refused_by_name(self, monkeypatch, unset_value):
        if unset_value is None:
            monkeypatch.delenv(KEYS_VARIABLE, raising=False)
        else:
            monkeypatch.setenv(KEYS_VARIABLE, unset_value)

        with pytest.raises(RegistrationTokenKeysNotConfigured) as refused:
            registration_token_cipher()

        assert str(refused.value) == f"{KEYS_VARIABLE} is not set"

    def test_a_value_that_is_not_a_fernet_key_is_refused_without_echoing_it(self, monkeypatch):
        not_a_key = "not-a-fernet-key-but-maybe-a-secret"
        monkeypatch.setenv(KEYS_VARIABLE, f"{KEY},{not_a_key}")

        with pytest.raises(RegistrationTokenKeysNotConfigured) as refused:
            registration_token_cipher()

        assert KEYS_VARIABLE in str(refused.value)
        assert not_a_key not in str(refused.value)
