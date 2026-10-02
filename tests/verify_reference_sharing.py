"""Verify against the reference project's actual Rust module, without editing it.

Run: .venv/bin/python tests/verify_reference_sharing.py --reference-root /path/to/your-szu-life
Cargo, its cached dependencies, and the reference checkout are needed only here.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from szu_electricity.catalog import empty_catalog
from szu_electricity.models import Location, Option
from szu_electricity.sharing import decode, encode


def verify(reference: Path, output: Path | None):
    module = reference / "src-tauri/src/campus_payments/electricity/sharing.rs"
    model = reference / "src-tauri/src/campus_payments/model.rs"
    # Use the exact production struct declaration and serde attributes, not a
    # handwritten approximation of the JSON shape.
    model_text = model.read_text()
    end = model_text.index("\n}", model_text.index("struct ElectricityLocationSettings")) + 2
    location_definition = model_text[:end]
    cases = [
        Location("yuehai", "yuehai-main", "54", "山茶斋", "0601"),
        Location("yuehai", "yuehai-xinzhai", "7126", "风槐斋", "0801"),
        Location("canghai", "canghai-main", "6875", "春笛3-8楼", "0501"),
        Location("xili", "xili-main", "10057", "A栋风信子", "0701"),
        Location("xili", "xili-lihuo-phase2", "01", "梧桐树", "501"),
    ]
    # Exercise every remainder size in Base16384's 7-byte blocks too.
    cases += [Location("yuehai", "yuehai-main", "54", "山茶斋", "0" * n + "601") for n in range(7)]
    catalog = empty_catalog()
    for location in cases:
        area = next(a for a in catalog.areas if a.id == location.areaId)
        option = Option(location.buildingId, location.buildingName)
        if option not in area.buildings:
            area.buildings.append(option)
    rust_catalog = [
        {
            "id": c.id,
            "areas": [
                {"id": a.id, "buildings": [asdict(b) for b in a.buildings]}
                for a in catalog.areas
                if a.campus_id == c.id
            ],
        }
        for c in catalog.campuses
    ]
    source = r"""
#![allow(dead_code)]
__LOCATION__
#[derive(Deserialize)]
pub struct ElectricityCampus { id: String, areas: Vec<ElectricityArea> }
#[derive(Deserialize)]
pub struct ElectricityArea { id: String, buildings: Vec<ElectricityBuilding> }
#[derive(Deserialize)]
pub struct ElectricityBuilding { id: String, name: String }
#[path = __MODULE__]
mod sharing;
fn main() {
    use std::io::{self, BufRead};
    for line in io::stdin().lock().lines() {
        let request: serde_json::Value = serde_json::from_str(&line.unwrap()).unwrap();
        let settings: ElectricityLocationSettings = serde_json::from_value(request["location"].clone()).unwrap();
        let options: Vec<ElectricityCampus> = serde_json::from_value(request["catalog"].clone()).unwrap();
        let imported = sharing::decode_settings(request["python_code"].as_str().unwrap()).unwrap();
        assert_eq!(settings, imported);
        sharing::validate_options(&imported, &options).unwrap();
        let exported = sharing::encode_settings(settings).unwrap();
        println!("{}", serde_json::json!({"code": exported, "location": imported}));
    }
}
""".replace("__LOCATION__", location_definition).replace(
        "__MODULE__", json.dumps(str(module.resolve()), ensure_ascii=False)
    )
    with tempfile.TemporaryDirectory(prefix="szu-reference-sharing-") as temp:
        root = Path(temp)
        (root / "src").mkdir()
        (root / "src/main.rs").write_text(source)
        (root / "Cargo.toml").write_text("""[package]
name = "szu-reference-sharing"
version = "0.1.0"
edition = "2021"
[dependencies]
base16384 = "=0.1.0"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
""")
        subprocess.run(
            ["cargo", "build", "--offline", "--quiet", "--manifest-path", str(root / "Cargo.toml")],
            check=True,
        )
        requests = [
            json.dumps(
                {"location": asdict(s), "python_code": encode(s), "catalog": rust_catalog},
                ensure_ascii=False,
            )
            for s in cases
        ]
        process = subprocess.run(
            [str(root / "target/debug/szu-reference-sharing")],
            input="\n".join(requests) + "\n",
            text=True,
            capture_output=True,
            check=True,
        )
        results = [json.loads(line) for line in process.stdout.splitlines()]
        assert len(results) == len(cases)
        for settings, result in zip(cases, results, strict=True):
            assert result["location"] == asdict(settings)
            assert encode(settings) == result["code"]
            assert decode(result["code"]) == settings
            assert catalog.validate(decode(result["code"])) == settings
    proof = {
        "reference": "your-szu-life",
        "sharing_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "location_definition_sha256": hashlib.sha256(location_definition.encode()).hexdigest(),
        "base16384_rust_version": "0.1.0",
        "vectors": results,
    }
    if output:
        output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
    print(
        f"REFERENCE_SHARING_BOTH_DIRECTIONS_OK: {len(cases)} cases across 5 areas; encode/decode and catalog validation passed"
    )
    print("sharing.rs SHA256:", proof["sharing_sha256"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--write-fixtures", type=Path)
    args = parser.parse_args()
    verify(args.reference_root, args.write_fixtures)
