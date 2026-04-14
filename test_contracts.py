import unittest


class TestXMemoryContracts(unittest.TestCase):
    def test_memory_db_init_is_canonical_owner(self):
        import memory_db
        import memory_store

        self.assertIs(memory_db.init_v6_stack, memory_db.init_v6_stack)
        self.assertTrue(callable(memory_db.init_db))
        self.assertTrue(callable(memory_store.init_db))

    def test_memory_store_init_delegates_to_memory_db(self):
        import memory_db
        import memory_store
        from unittest.mock import patch

        with patch.object(memory_db, "init_db") as mock_init:
            memory_store.init_db()
            mock_init.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
