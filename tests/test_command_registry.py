import ast
import unittest
from pathlib import Path


BOT_PATH = Path(__file__).resolve().parents[1] / "bot.py"


def registered_commands() -> tuple[dict[str, ast.AsyncFunctionDef], list[str]]:
    module = ast.parse(BOT_PATH.read_text(encoding="utf-8"))
    functions: dict[str, ast.AsyncFunctionDef] = {}
    names: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            command = decorator.func
            if not (
                isinstance(command, ast.Attribute)
                and command.attr == "command"
                and isinstance(command.value, ast.Attribute)
                and command.value.attr == "tree"
                and isinstance(command.value.value, ast.Name)
                and command.value.value.id == "bot"
            ):
                continue
            name = next(
                (
                    keyword.value.value
                    for keyword in decorator.keywords
                    if keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                None,
            )
            if name:
                names.append(name)
                functions[name] = node
    return functions, names


class CommandRegistryTests(unittest.TestCase):
    def test_slash_command_names_are_unique(self):
        _functions, names = registered_commands()
        self.assertGreater(len(names), 0)
        self.assertEqual(len(names), len(set(names)))

    def test_info_accepts_an_optional_employee_profile_target(self):
        functions, _names = registered_commands()
        args = functions["info"].args
        argument_names = [argument.arg for argument in args.args]

        self.assertIn("membre", argument_names)
        self.assertIsInstance(args.defaults[-1], ast.Constant)
        self.assertIsNone(args.defaults[-1].value)

    def test_member_target_commands_have_consistent_target_arguments(self):
        functions, _names = registered_commands()
        target_commands = {
            "mystats",
            "recruter",
            "averto",
            "delaverto",
            "kickj",
            "horaire",
        }

        for command_name in target_commands:
            with self.subTest(command=command_name):
                arguments = [
                    argument.arg for argument in functions[command_name].args.args
                ]
                self.assertIn("membre", arguments)

        recruiter_arguments = [
            argument.arg for argument in functions["recruter"].args.args
        ]
        self.assertIn("decision", recruiter_arguments)

    def test_removed_commands_stay_removed(self):
        _functions, names = registered_commands()
        removed = {
            "infoall", "assistant", "analyser_candidature", "article", "recap",
            "below5", "noservice", "addplaque", "delplaque", "checkallplaques",
            "blinteraction", "delblinteraction", "service", "configurer",
        }
        self.assertFalse(removed & set(names))


if __name__ == "__main__":
    unittest.main()
