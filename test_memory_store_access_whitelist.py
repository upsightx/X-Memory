import unittest
from pathlib import Path


class TestMemoryStoreAccessWhitelist(unittest.TestCase):
    def test_only_allowed_files_import_memory_store_directly(self):
        root = Path(__file__).resolve().parent
        allowed = {
            "memory_db.py",
            "memory_store.py",
            "README.md",
            "test_contracts.py",
            "test_memory_store_access_whitelist.py",
        }

        offenders = []
        for path in root.iterdir():
            if not path.is_file() or path.suffix not in {".py", ".md"}:
                continue
            text = path.read_text(encoding="utf-8")
            if (
                "from memory_store import" in text
                or "import memory_store" in text
                or "memory_store." in text
            ) and path.name not in allowed:
                offenders.append(path.name)

        self.assertEqual(offenders, [], f"unexpected direct memory_store access: {offenders}")


if __name__ == "__main__":
    unittest.main()
