from cua.core.actions import SecretRef
from cua.safety.redact import Redactor


def test_pattern_rules():
    r = Redactor()
    out = r.text("SSN 123-45-6789, call (414) 555-0142, mail a@b.com, bal $2,450.18")
    assert out == "SSN [SSN], call [PHONE], mail [EMAIL], bal [AMOUNT]"


def test_masked_ssn_last4_is_left_alone():
    assert Redactor().text("***-**-0000") == "***-**-0000"


def test_card_vs_account_numbers():
    r = Redactor()
    assert r.text("card 4111 1111 1111 1111") == "card [CARD]"      # Luhn-valid
    assert r.text("acct 123456789") == "acct [ACCT]"                 # 9+ digits


def test_member_id_is_not_redacted():
    # 5-digit member ids are operational identifiers the logs must keep.
    assert Redactor().text("MBR-404 NO MEMBER FOUND MATCHING 99999").endswith("99999")


def test_registered_values_scrubbed_everywhere():
    r = Redactor()
    r.register("JANE Q SAMPLE", "pii")
    assert r.text("Name: JANE Q SAMPLE") == "Name: [PII]"


def test_recursive_and_secret_refs():
    r = Redactor()
    r.register("hunter2", "secret")
    out = r.obj({"a": ["x hunter2 y", {"b": SecretRef(secret="MOCK_PASS")}], "n": 3})
    assert out == {"a": ["x [SECRET] y", {"b": "[SECRET:MOCK_PASS]"}], "n": 3}


def test_registered_values_become_screenshot_masks():
    r = Redactor()
    r.register("JANE Q SAMPLE", "pii")
    assert "JANE\\ Q\\ SAMPLE" in r.mask_patterns()
