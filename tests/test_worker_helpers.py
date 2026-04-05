from api.app.worker import _extract_clave_value
from sri_scraper.parser import parse_clave


def test_extract_clave_value_supports_dict_payload():
    assert _extract_clave_value({"clave": "1234567890"}) == "1234567890"


def test_extract_clave_value_supports_parsed_clave_object():
    clave = parse_clave("2303202601179071031900129300010000138985658032315")
    assert _extract_clave_value(clave) == "2303202601179071031900129300010000138985658032315"
