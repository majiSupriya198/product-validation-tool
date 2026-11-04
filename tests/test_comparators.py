import pytest

from comparators.base import CompareContext
from comparators.exact import compare_boolean, compare_exact, compare_name
from comparators.keyvalue import compare_keyvalue
from comparators.listset import compare_images, compare_list
from comparators.nested import compare_nested
from comparators.numeric import compare_numeric
from comparators.text import compare_text
from utils.normalization import parse_number, split_items

CTX = CompareContext("x")


@pytest.mark.parametrize("raw,expected", [
    ("1.99", 1.99), ("1.99 €", 1.99), ("13,27 € / 1kg", 13.27), ("1.234,56", 1234.56),
    ("1,234.56", 1234.56), ("0.15 KG", 0.15), (2, 2.0), ("abc", None), (None, None),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_split_items_keeps_brackets_and_decimal_commas():
    s = "Mais* 85%, Kokosöl* 13, 5%, Meersalz 1, 5% *aus biologischem Anbau, Öle (Sonnenblume, Raps)"
    assert split_items(s) == ["Mais* 85%", "Kokosöl* 13,5%", "Meersalz 1,5% *aus biologischem Anbau", "Öle (Sonnenblume, Raps)"]


def test_numeric_flink_price_formats():
    assert compare_numeric("1.99", "1.99 €", CTX).score == 1
    assert compare_numeric("13.27 € / 1KG", "13,27 € / 1kg", CTX).score == 1
    r = compare_numeric("1.99", "2.29 €", CTX)
    assert r.score == 0 and r.evidence["delta"] == pytest.approx(0.30)


def test_numeric_unit_mismatch():
    r = compare_numeric("0.75 LITER", "1 UNIT", CTX)
    assert r.score == 0 and r.evidence["unit_mismatch"]


def test_numeric_against_list():
    assert compare_numeric("5.03", [5.03], CTX).score == 1


def test_exact_and_name():
    assert compare_exact("Funny-frisch", "funny frisch", CTX).score == 1
    assert compare_exact("Deutschland", "Österreich", CTX).score == 0
    r = compare_name("Vita Coco Coconut Water 330ml", "Vita Coco Coconut water 33cl", CTX)
    assert 0.6 < r.score < 0.7 and "330" in r.missing


def test_boolean():
    assert compare_boolean("False", False, CTX).score == 1
    assert compare_boolean("True", False, CTX).score == 0


def test_ingredients_six_of_eight_is_75_percent():
    up = "A, B, C, D, E, F, G, H"
    prod = "A, B, C, D, E, F"
    r = compare_list(up, prod, CTX)
    assert r.score == pytest.approx(0.75)
    assert r.missing == ["G", "H"] and r.extra == []


def test_list_near_match():
    r = compare_list("pflanzliche Öle (Sonnenblume, Raps), Salz", "pflanzliche Öle (Sonnenblume, Raps in veränderlichen Anteilen), Salz",
                     CompareContext("x", item_threshold=0.5))
    assert r.score == 1 and r.evidence["near_matches"]


def test_nutrition_pipe_vs_comma():
    up = "Nährwerte (je 100 g): Energie: 2,223 kJ/ 533 kcal | Fett: 33 g | - davon Zucker: 2,5 g | Salz: 1,5 g"
    prod = "Energie: 2,223 kJ/ 533 kcal, Fett: 33 g, - davon Zucker: 2,5 g, Salz: 1,5 g"
    assert compare_keyvalue(up, prod, CTX).score == 1


def test_nutrition_partial_explains():
    up = "Energie: 533 kcal | Fett: 33 g | Kohlenhydrate: 49 g | Eiweiß: 6 g | Salz: 1,5 g"
    prod = "Energie: 533 kcal, Fett: 30 g, Kohlenhydrate: 50 g, Eiweiß: 6 g"
    r = compare_keyvalue(up, prod, CTX)
    assert r.score == pytest.approx(2 / 5)
    assert r.evidence["matched"] == ["Energie", "Eiweiß"]
    assert len(r.evidence["differ"]) == 2 and r.missing == ["Salz: 1,5 g"]


def test_nutrition_header_without_colon():
    up = "Nährwerte bei 100 g Energie: 2437 kJ/ 587 kcal | Fett: 45 g"
    prod = "Energie: 2437 kJ/ 587 kcal, Fett: 45 g"
    assert compare_keyvalue(up, prod, CTX).score == 1


def test_images_rehosted_counts_only():
    up = ["https://res.cloudinary.com/a/1.png", "https://res.cloudinary.com/a/2.png"]
    prod = ["https://gather-assets-prod.gather.commerceiq.ai/x/1.res"]
    r = compare_images(up, prod, CTX)
    assert r.score == 0.5 and r.evidence["rehosted"]


def test_images_same_host_set_match():
    up = ["https://cdn.x.com/1.png", "https://cdn.x.com/2.png"]
    prod = ["https://cdn.x.com/1.png?w=200"]
    r = compare_images(up, prod, CTX)
    assert r.score == 0.5 and r.missing == ["https://cdn.x.com/2.png"]


def test_text_word_f1():
    r = compare_text("Kartoffelchips mit rauchigem Paprika-Geschmack", "Kartoffelchips mit Paprika", CTX)
    # tokens: up 5, prod 3, common 3 -> 2*3/8
    assert r.score == pytest.approx(0.75)
    assert "rauchigem" in r.missing


def test_text_case_only_difference_is_match():
    assert compare_text("Vor Wärme  geschützt.", "vor wärme geschützt", CTX).score == 1


def test_nested_comments_by_review_id():
    up = [{"reviewId": "1", "rating": 5, "content": "gut"}, {"reviewId": "2", "rating": 4, "content": "ok"}]
    prod = [{"reviewId": "1", "rating": 5.0, "content": "gut"}]
    r = compare_nested(up, prod, CTX)
    assert r.score == 0.5 and r.missing == ["reviewId=2"]
