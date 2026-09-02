"""`bound_surface` reports the names a sealed suite binds, and never its values.

The builder is handed the public contract and the sealed digest, and then has to
guess which function the hidden cases call and which keys they read off the
result. On the WP7 run it guessed nineteen times and shipped six aliases of one
function in a single file. These cases pin the two halves of the fix: the names
that must come out, and the values that must not.

The fixtures here are synthetic. They reproduce the *forms* of the live sealed
suites — a runtime `loadModule()` indirection so an unwritten module fails an
assertion rather than the collector, expectations hoisted into constants, a
regex carrying a quote — without copying sealed content into this repository.
"""

from __future__ import annotations

import json
import re
import unittest

from adw_modules.bound_surface import derive_bound_surface


VITEST_SUITE = '''
import { readFileSync } from "node:fs";
import type { APIContext } from "astro";
import defaultExport from "../support/harness";
import * as contracts from "../api/contracts";
import { describe, expect, it } from "vitest";
import "./side-effect-only";

// A module the build lane has not written yet is loaded at runtime so an
// unresolved specifier fails an assertion instead of the collector.
async function loadModule(relativeSpecifier: string): Promise<any> {
  const specifier = ["./", relativeSpecifier].join("");
  try {
    return await import(/* @vite-ignore */ specifier);
  } catch {
    return null;
  }
}

/* Takes a string, never imports anything: not a loader. */
function readSource(relativePath: string): string {
  return readFileSync(relativePath, "utf8");
}

const GOLDEN_BAND = {
  reaction: "anaphylaxis",
  reportCount: 12,
  observation: "reported 12 times in ADVERSE REACTIONS",
};

describe("paid surface", () => {
  it("builds the entity surface", async () => {
    const paidDpa = await loadModule("paid-dpa");
    const buildEntityDpaSurface = paidDpa?.buildEntityDpaSurface;
    expect(typeof buildEntityDpaSurface).toBe("function");

    const surface = buildEntityDpaSurface({ entitled: false, dpa: null });
    expect(surface.publicCharts).toEqual([]);
    expect(surface.paidPanel.entitled).toBe(false);

    const panel = paidDpa!.buildPaidPanel({ entitled: false });
    expect(panel).toEqual({
      entitled: false,
      metrics: null,
      matrix: null,
      fields: [],
      checkoutCta: { href: "/checkout", label: "Open checkout" },
    });

    const bandModule = await loadModule("../seo/public-band");
    const missing = bandModule?.mapPublicBand(null);
    expect(missing.available).toBe(false);
    expect(missing.message).toBe("Label section evidence is unavailable.");

    expect(bandModule!.mapPublicBand(GOLDEN_BAND)).toEqual(GOLDEN_BAND);
    expect(contracts.FAERS_ANALYTICS_DIMENSIONS).toContain("reaction");
    expect(defaultExport.harnessName).toBe("wp7");

    const source = readSource("src/pages/checkout/success.astro");
    expect(source).toMatch(/path:\\s*"\\/"/);
    expect(source).toMatch(/sameSite:\\s*"lax"/i);
    expect(source).toContain(`cookie ${"fdadb_entitlement"} set`);
  });
});
'''


PYTEST_SUITE = '''
from __future__ import annotations

import json

import httpx

from app.mapping import FAERS_SCOPES, source_scope

EXPECTED_METRICS = {"prr": 3.21, "prrCiLow": 1.83, "ebgm": None}
EXPECTED_CONTINGENCY = {"a": 12, "b": 240, "n": 52000}
NESTED = {"release": {"id": "FAERS-2024Q4", "manifestSha256": "aaaa"}}


def test_scope_is_not_public() -> None:
    assert FAERS_SCOPES["dpa"] != "summary:read"
    assert source_scope("FAERS", "dpa") == FAERS_SCOPES["dpa"]
    assert {"drug": "dupixent", "reaction": "anaphylaxis"} == request_body()


def test_mapper() -> None:
    import app.mapping as mapping

    mapped = mapping.normalize_dpa_data(upstream())
    assert mapped["metrics"] == EXPECTED_METRICS
    assert mapped["contingency"] == EXPECTED_CONTINGENCY
    assert mapped["queryHash"] == "b" * 64
    assert mapped.release_id == "FAERS-2024Q4"
    assert NESTED == mapped["envelope"]
'''


def _all_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _all_strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _all_strings(item)]
    return []


def _module(surface: dict, specifier: str) -> dict | None:
    for entry in surface["modules"]:
        if entry["specifier"] == specifier:
            return entry
    return None


class ShapeTest(unittest.TestCase):
    def test_empty_input_returns_empty_lists_never_none(self) -> None:
        surface = derive_bound_surface({})
        self.assertEqual(surface, {"modules": [], "object_keys": []})

    def test_unrecognised_extensions_contribute_nothing(self) -> None:
        surface = derive_bound_surface(
            {
                "specs/fixture.json": '{"import": "x", "prr": 1}',
                "README.md": "import { a } from 'b'",
                "run.sh": "import x",
            }
        )
        self.assertEqual(surface, {"modules": [], "object_keys": []})

    def test_output_is_sorted_at_every_level(self) -> None:
        surface = derive_bound_surface(
            {
                "src/a.test.ts": VITEST_SUITE,
                "services/tests/test_b.py": PYTEST_SUITE,
            }
        )
        specifiers = [entry["specifier"] for entry in surface["modules"]]
        self.assertEqual(specifiers, sorted(specifiers))
        self.assertEqual(surface["object_keys"], sorted(surface["object_keys"]))
        for entry in surface["modules"]:
            self.assertEqual(entry["symbols"], sorted(entry["symbols"]))

    def test_result_is_deterministic_across_input_ordering(self) -> None:
        first = derive_bound_surface(
            {"src/a.test.ts": VITEST_SUITE, "t/test_b.py": PYTEST_SUITE}
        )
        second = derive_bound_surface(
            {"t/test_b.py": PYTEST_SUITE, "src/a.test.ts": VITEST_SUITE}
        )
        self.assertEqual(first, second)

    def test_unparseable_python_contributes_nothing_rather_than_guessing(self) -> None:
        surface = derive_bound_surface({"t/test_broken.py": "def (:\n  import ,,"})
        self.assertEqual(surface, {"modules": [], "object_keys": []})

    def test_truncated_javascript_does_not_raise(self) -> None:
        for source in (
            'import { a } from "m',
            "const x = `unterminated ${",
            "/* never closed",
            'const r = /unterminated"',
            "const o = { a: 1, b: { c:",
        ):
            with self.subTest(source=source):
                derive_bound_surface({"src/a.test.ts": source})


class JavascriptModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.surface = derive_bound_surface({"src/lib/seo/paid.test.ts": VITEST_SUITE})

    def test_named_import_reports_its_symbols(self) -> None:
        self.assertEqual(
            _module(self.surface, "vitest"),
            {"specifier": "vitest", "symbols": ["describe", "expect", "it"]},
        )

    def test_type_only_import_is_a_bound_name(self) -> None:
        self.assertEqual(
            _module(self.surface, "astro"),
            {"specifier": "astro", "symbols": ["APIContext"]},
        )

    def test_namespace_import_reports_dereferenced_symbols(self) -> None:
        self.assertEqual(
            _module(self.surface, "src/lib/api/contracts"),
            {
                "specifier": "src/lib/api/contracts",
                "symbols": ["FAERS_ANALYTICS_DIMENSIONS"],
            },
        )

    def test_default_import_reports_default_and_its_dereferenced_names(self) -> None:
        self.assertEqual(
            _module(self.surface, "src/lib/support/harness"),
            {
                "specifier": "src/lib/support/harness",
                "symbols": ["default", "harnessName"],
            },
        )

    def test_side_effect_import_reports_the_module_with_no_symbols(self) -> None:
        self.assertEqual(
            _module(self.surface, "src/lib/seo/side-effect-only"),
            {"specifier": "src/lib/seo/side-effect-only", "symbols": []},
        )

    def test_relative_specifier_resolves_against_the_sealed_file(self) -> None:
        self.assertIsNotNone(_module(self.surface, "src/lib/api/contracts"))
        self.assertIsNone(_module(self.surface, "../api/contracts"))

    def test_package_specifier_is_left_exactly_as_written(self) -> None:
        for specifier in ("vitest", "astro", "node:fs"):
            with self.subTest(specifier=specifier):
                self.assertIsNotNone(_module(self.surface, specifier))


class LoaderHelperTest(unittest.TestCase):
    """The form the live WP7 suite uses to stay red at the parent commit."""

    def setUp(self) -> None:
        self.surface = derive_bound_surface({"src/lib/seo/paid.test.ts": VITEST_SUITE})

    def test_runtime_loaded_module_is_reported_with_its_dereferenced_symbols(
        self,
    ) -> None:
        self.assertEqual(
            _module(self.surface, "src/lib/seo/paid-dpa"),
            {
                "specifier": "src/lib/seo/paid-dpa",
                "symbols": ["buildEntityDpaSurface", "buildPaidPanel"],
            },
        )

    def test_relative_specifier_through_the_loader_resolves_too(self) -> None:
        self.assertEqual(
            _module(self.surface, "src/lib/seo/public-band"),
            {"specifier": "src/lib/seo/public-band", "symbols": ["mapPublicBand"]},
        )

    def test_a_function_that_never_imports_is_not_a_loader(self) -> None:
        # `readSource("src/pages/checkout/success.astro")` takes a string and
        # returns a file's text. Reading its argument as a module specifier
        # would invent a module and leak a path.
        for entry in self.surface["modules"]:
            self.assertNotIn("success", entry["specifier"])
            self.assertNotIn("checkout", entry["specifier"])


class ObjectKeyTest(unittest.TestCase):
    def keys(self, source: str, path: str = "src/lib/seo/paid.test.ts") -> list[str]:
        return derive_bound_surface({path: source})["object_keys"]

    def test_shape_matcher_literal_keys_are_reported(self) -> None:
        keys = self.keys(VITEST_SUITE)
        for key in ("entitled", "metrics", "matrix", "fields", "checkoutCta"):
            self.assertIn(key, keys)

    def test_nested_literal_keys_are_reported(self) -> None:
        self.assertIn("href", self.keys(VITEST_SUITE))
        self.assertIn("label", self.keys(VITEST_SUITE))

    def test_literal_values_are_never_read_as_keys(self) -> None:
        keys = self.keys('expect(x).toEqual({ entitled: false, metrics: null });')
        self.assertEqual(keys, ["entitled", "metrics"])

    def test_spread_operand_is_not_a_key(self) -> None:
        keys = self.keys("expect(x).toEqual({ ...BASE, prr: 1 });")
        self.assertEqual(keys, ["prr"])

    def test_shorthand_property_is_a_key(self) -> None:
        self.assertEqual(self.keys("expect(x).toEqual({ entitled, metrics });"),
                         ["entitled", "metrics"])

    def test_a_named_constant_passed_to_a_shape_matcher_is_resolved(self) -> None:
        keys = self.keys(
            'const EXPECTED = { prr: 4.2, ebgm: null };\n'
            "expect(panel.metrics).toEqual(EXPECTED);\n"
        )
        # `panel` is a local of unknown provenance, so `.metrics` is not claimed;
        # only the resolved constant's own keys are.
        self.assertEqual(keys, ["ebgm", "prr"])

    def test_value_matchers_do_not_contribute_their_argument(self) -> None:
        self.assertEqual(self.keys('expect(x).toBe("dupixent");'), [])
        self.assertEqual(self.keys('expect(x).toContain("anaphylaxis");'), [])

    def test_result_shape_of_a_first_party_symbol_is_reported(self) -> None:
        keys = self.keys(VITEST_SUITE)
        for key in ("publicCharts", "paidPanel", "available", "message"):
            self.assertIn(key, keys)

    def test_shape_of_a_package_return_value_is_not_tracked(self) -> None:
        # `readFileSync` comes from node:fs, so nothing read off its result is
        # part of the contract the builder has to satisfy.
        keys = self.keys(
            'import { readFileSync } from "node:fs";\n'
            'const text = readFileSync("a");\n'
            "expect(text.byteOffset).toBe(0);\n"
        )
        self.assertEqual(keys, [])


class PythonSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.surface = derive_bound_surface(
            {"services/api-gateway/tests/test_dpa.py": PYTEST_SUITE}
        )

    def test_from_import_reports_module_and_symbols(self) -> None:
        self.assertEqual(
            _module(self.surface, "app.mapping"),
            {
                "specifier": "app.mapping",
                "symbols": [
                    "FAERS_SCOPES",
                    "normalize_dpa_data",
                    "source_scope",
                ],
            },
        )

    def test_aliased_import_contributes_its_attribute_access(self) -> None:
        entry = _module(self.surface, "app.mapping")
        self.assertIn("normalize_dpa_data", entry["symbols"])

    def test_dotted_import_matches_the_longest_binding(self) -> None:
        surface = derive_bound_surface(
            {
                "t/test_a.py": (
                    "import app.mapping\n"
                    "def test_x():\n"
                    "    assert app.mapping.normalize_dpa_data({}) == {}\n"
                )
            }
        )
        self.assertEqual(
            _module(surface, "app.mapping"),
            {"specifier": "app.mapping", "symbols": ["normalize_dpa_data"]},
        )

    def test_relative_import_is_not_reported_rather_than_guessed(self) -> None:
        surface = derive_bound_surface(
            {"services/api/tests/test_a.py": "from ..mapping import normalize\n"}
        )
        self.assertEqual(surface["modules"], [])

    def test_dict_literal_keys_in_a_comparison_are_reported(self) -> None:
        for key in ("drug", "reaction"):
            self.assertIn(key, self.surface["object_keys"])

    def test_a_named_dict_constant_in_a_comparison_is_resolved(self) -> None:
        for key in ("prr", "prrCiLow", "ebgm", "a", "b", "n"):
            self.assertIn(key, self.surface["object_keys"])

    def test_nested_dict_keys_are_reported(self) -> None:
        for key in ("release", "id", "manifestSha256"):
            self.assertIn(key, self.surface["object_keys"])

    def test_result_shape_of_a_first_party_symbol_is_reported(self) -> None:
        for key in ("metrics", "contingency", "queryHash", "release_id"):
            self.assertIn(key, self.surface["object_keys"])

    def test_stdlib_result_shape_is_not_tracked(self) -> None:
        surface = derive_bound_surface(
            {
                "t/test_a.py": (
                    "import json\n"
                    "def test_x():\n"
                    '    parsed = json.loads("{}")\n'
                    '    assert parsed["jwt"]["alg"] == "HS256"\n'
                )
            }
        )
        self.assertEqual(surface["object_keys"], [])


#: Every distinctive value in `LEAKY_TS` and `LEAKY_PY` below. Each appears only
#: in a value position, so any one of them turning up in the output is a leak.
LEAK_MARKERS = (
    "zqx-string-value",
    "zqx-template-value",
    "zqx-regex-value",
    "zqx-nested-value",
    "zqx-array-value",
    "zqx-comment-value",
    "zqx-computed-value",
    "zqx-default-value",
    "zqx-fixture-value",
    "zqx-subscript-value",
    "zqx-docstring-value",
    "zqx-header-value",
    "8675309",
    "3.14159",
)

LEAKY_TS = '''
// zqx-comment-value
import { expect, it } from "vitest";

const FIXTURE = {
  reaction: "zqx-fixture-value",
  counts: [8675309, "zqx-array-value"],
  nested: { verbatim: "zqx-nested-value", ratio: 3.14159 },
  ["computed"]: "zqx-computed-value",
};

async function loadModule(spec: string) {
  return await import(spec);
}

it("leaks nothing", async () => {
  const paidDpa = await loadModule("paid-dpa");
  const surface = paidDpa.buildEntityDpaSurface({ entitled: true });
  expect(surface.available).toBe("zqx-string-value");
  expect(surface.headers["zqx-header-value"]).toBe(8675309);
  expect(`x ${"zqx-template-value"} y`).toMatch(/zqx-regex-value/);
  expect(surface).toEqual({
    available: true,
    label: "zqx-default-value",
    matrix: { a: 8675309, b: "zqx-subscript-value" },
  });
  expect(FIXTURE).toEqual(FIXTURE);
});
'''

LEAKY_PY = '''
"""zqx-docstring-value"""

from app.mapping import normalize_dpa_data

FIXTURE = {
    "reaction": "zqx-fixture-value",
    "counts": [8675309, "zqx-array-value"],
    "nested": {"verbatim": "zqx-nested-value", "ratio": 3.14159},
}

HEADERS = {"zqx-header-value": "zqx-string-value"}


def test_leaks_nothing():
    mapped = normalize_dpa_data(FIXTURE)
    assert mapped["metrics"] == FIXTURE
    assert mapped.available == "zqx-template-value"
    assert HEADERS == {"zqx-header-value": "zqx-computed-value"}
    assert mapped["zqx-subscript-value"] != "zqx-default-value"
'''


class NoValueEverLeaksTest(unittest.TestCase):
    """The rule the whole module exists to hold: names out, values never."""

    def setUp(self) -> None:
        self.surface = derive_bound_surface(
            {
                "src/lib/seo/leaky.test.ts": LEAKY_TS,
                "services/tests/test_leaky.py": LEAKY_PY,
            }
        )
        self.serialized = json.dumps(self.surface)

    def test_the_extractor_actually_found_something(self) -> None:
        # A leak test over an empty result proves nothing.
        self.assertIn("available", self.surface["object_keys"])
        self.assertIn(
            "src/lib/seo/paid-dpa",
            [entry["specifier"] for entry in self.surface["modules"]],
        )

    def test_no_asserted_value_appears_anywhere_in_the_output(self) -> None:
        for marker in LEAK_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.serialized)

    def test_a_string_keyed_subscript_that_is_not_an_identifier_is_dropped(
        self,
    ) -> None:
        self.assertNotIn("zqx-header-value", self.surface["object_keys"])

    def test_every_symbol_and_key_is_an_identifier(self) -> None:
        identifier = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
        names = list(self.surface["object_keys"])
        for entry in self.surface["modules"]:
            names.extend(entry["symbols"])
        for name in names:
            with self.subTest(name=name):
                self.assertRegex(name, identifier)

    def test_every_specifier_is_a_path_or_package_name(self) -> None:
        specifier = re.compile(r"^[A-Za-z0-9_@][A-Za-z0-9_.:/\\@-]*$")
        for entry in self.surface["modules"]:
            with self.subTest(specifier=entry["specifier"]):
                self.assertRegex(entry["specifier"], specifier)

    def test_no_output_string_contains_whitespace_or_a_quote(self) -> None:
        for value in _all_strings(self.surface):
            with self.subTest(value=value):
                self.assertNotRegex(value, r"[\s'\"`]")


if __name__ == "__main__":
    unittest.main()
