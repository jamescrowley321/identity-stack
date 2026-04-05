"""Unit tests for IdentityService ABC contract enforcement.

Verifies that the ABC contract is enforced at instantiation time:
a concrete subclass missing any method cannot be instantiated.
"""

from app.services.identity import IdentityService


class TestConcreteSubclassMustImplementAll:
    """A subclass missing any method should raise TypeError on instantiation."""

    def test_partial_implementation_fails(self):
        class PartialService(IdentityService):
            async def create_user(self, **kwargs):
                pass  # Only one method implemented

        try:
            PartialService()  # type: ignore[abstract]
            assert False, "Should not be able to instantiate partial implementation"
        except TypeError:
            pass
