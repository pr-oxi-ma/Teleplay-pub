import ast
import unittest
from pathlib import Path


class StreamingContractTests(unittest.TestCase):
    def test_stream_file_accepts_and_forwards_concurrency(self):
        source_path = Path(__file__).resolve().parents[1] / "app" / "streaming.py"
        module = ast.parse(source_path.read_text(encoding="utf-8"))

        stream_function = next(
            node
            for node in module.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "stream_file"
        )
        parameter_names = [argument.arg for argument in stream_function.args.args]
        self.assertIn("concurrency", parameter_names)

        parallel_call = next(
            node
            for node in ast.walk(stream_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "parallel_stream_generator"
        )
        concurrency_keyword = next(
            (keyword for keyword in parallel_call.keywords if keyword.arg == "concurrency"),
            None,
        )
        self.assertIsNotNone(concurrency_keyword)
        self.assertIsInstance(concurrency_keyword.value, ast.Name)
        self.assertEqual(concurrency_keyword.value.id, "concurrency")


if __name__ == "__main__":
    unittest.main()
