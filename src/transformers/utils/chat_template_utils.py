# Copyright 2024 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import json
import re
from typing import Any, Union, get_args, get_origin, get_type_hints


BASIC_TYPES = (int, float, str, bool, Any, type(None), ...)


def get_json_schema(func):
    """
    This function generates a JSON schema for a given function, based on its docstring and type hints. This is
    mostly used for passing lists of tools to a chat template. The JSON schema contains the name and description of
    the function, as well as the names, types and descriptions for each of its arguments. `get_json_schema()` requires
    that the function has a docstring, and that each argument has a description in the docstring, in the standard
    Google docstring format shown below. It also requires that all the function arguments have a valid Python type hint.

    Although it is not required, a `Returns` block can also be added, which will be included in the schema. This is
    optional because most chat templates ignore the return value of the function. Each argument description
    can also have an optional `(choices: ...)` block at the end, such as `(choices: ["tea", "coffee"])`, which will be
    parsed into an `enum` field in the schema. Note that this will only be parsed correctly if it is at the end of the
    line.

    Args:
        func: The function to generate a JSON schema for.

    Returns:
        A dictionary containing the JSON schema for the function.

    Examples:
    ```python
    >>> def multiply(x: float, y: float):
    >>>    '''
    >>>    A function that multiplies two numbers
    >>>
    >>>    Args:
    >>>        x: The first number to multiply
    >>>        y: The second number to multiply
    >>>    '''
    >>>    return x * y
    >>>
    >>> print(get_json_schema(multiply))
    {
        "name": "multiply",
        "description": "A function that multiplies two numbers",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "The first number to multiply"},
                "y": {"type": "number", "description": "The second number to multiply"}
            },
            "required": ["x", "y"]
        }
    }
    ```

    The general use for these schemas is that they are used to generate tool descriptions for chat templates that
    support them, like so:

    ```python
    >>> from transformers import AutoTokenizer
    >>> from transformers.utils import get_json_schema
    >>>
    >>> def multiply(x: float, y: float):
    >>>    '''
    >>>    A function that multiplies two numbers
    >>>
    >>>    Args:
    >>>        x: The first number to multiply
    >>>        y: The second number to multiply
    >>>    return x * y
    >>>    '''
    >>>
    >>> multiply_schema = get_json_schema(multiply)
    >>> tokenizer = AutoTokenizer.from_pretrained("CohereForAI/c4ai-command-r-v01")
    >>> messages = [{"role": "user", "content": "What is 179 x 4571?"}]
    >>> formatted_chat = tokenizer.apply_chat_template(
    >>>                       messages,
    >>>                       tools=[multiply_schema],
    >>>                       chat_template="tool_use",
    >>>                       return_dict=True,
    >>>                       return_tensors="pt",
    >>>                       add_generation_prompt=True
    >>>                       )
    >>> # The formatted chat can now be passed to model.generate()
    ```

    In many cases, it is more convenient to simply pass the functions directly to apply_chat_template and let it
    autogenerate schemas than calling this function directly.
    """
    doc = inspect.getdoc(func)
    if not doc:
        raise ValueError(f"Cannot generate JSON schema for {func.__name__} because it has no docstring!")
    doc = doc.strip()
    main_doc, param_descriptions, return_doc = parse_google_format_docstring(doc)

    json_schema = _convert_type_hints_to_json_schema(func)
    if (return_dict := json_schema["properties"].pop("return", None)) is not None:
        if return_doc is not None:  # We allow a missing return docstring since most templates ignore it
            return_dict["description"] = return_doc
    for arg in json_schema["properties"]:
        if arg not in param_descriptions:
            raise ValueError(
                f"Cannot generate JSON schema for {func.__name__} because the docstring has no description for the argument '{arg}'"
            )
        desc = param_descriptions[arg]
        enum_choices = re.search(r"\(choices:\s*([^)]+)\)\s*$", desc, flags=re.IGNORECASE)
        if enum_choices:
            json_schema["properties"][arg]["enum"] = [c.strip() for c in json.loads(enum_choices.group(1))]
            desc = enum_choices.string[: enum_choices.start()].strip()
        json_schema["properties"][arg]["description"] = desc

    output = {"name": func.__name__, "description": main_doc, "parameters": json_schema}
    if return_dict is not None:
        output["return"] = return_dict
    return output


def parse_google_format_docstring(docstring):
    """
    Parses a Google-style docstring to extract the function description,
    argument descriptions, and return description.

    Args:
        docstring (str): The docstring to parse.

    Returns:
        dict: A dictionary containing the function description, arguments, and return description.
    """
    # Regular expressions to match the sections
    description_re = re.compile(r"^(.*?)[\n\s]*(Args:|Returns:|Raises:|\Z)", re.DOTALL)
    args_re = re.compile(r"\n\s*Args:\n\s*(.*?)[\n\s]*(Returns:|Raises:|\Z)", re.DOTALL)
    returns_re = re.compile(r"\n\s*Returns:\n\s*(.*?)[\n\s]*(Raises:|\Z)", re.DOTALL)

    # Extract the sections
    description_match = description_re.search(docstring)
    args_match = args_re.search(docstring)
    returns_match = returns_re.search(docstring)

    # Clean and store the sections
    description = description_match.group(1).strip() if description_match else None
    args = args_match.group(1).strip() if args_match else None
    returns = returns_match.group(1).strip() if returns_match else None

    # Parsing the arguments into a dictionary
    args_dict = {}
    if args is not None:
        arg_lines = args.split("\n")
        for line in arg_lines:
            arg_name, arg_desc = line.split(":", 1)
            args_dict[arg_name.strip()] = arg_desc.strip()

    return description, args_dict, returns


def _convert_type_hints_to_json_schema(func):
    type_hints = get_type_hints(func)
    properties = {}

    signature = inspect.signature(func)
    required = []
    for param_name, param in signature.parameters.items():
        if param.annotation == inspect.Parameter.empty:
            raise ValueError(f"Argument {param.name} is missing a type hint in function {func.__name__}")
        if param.default == inspect.Parameter.empty:
            required.append(param_name)

    for param_name, param_type in type_hints.items():
        properties[param_name] = _parse_type_hint(param_type)

    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    return schema


def _parse_type_hint(hint):
    if (origin := get_origin(hint)) is not None:
        if origin is Union:
            # If it's a union of basic types, we can express that as a simple list in the schema
            if all(t in BASIC_TYPES for t in get_args(hint)):
                return_dict = {
                    "type": [_get_json_schema_type(t)["type"] for t in get_args(hint) if t not in (type(None), ...)]
                }
                if len(return_dict["type"]) == 1:
                    return_dict["type"] = return_dict["type"][0]
            else:
                # A union of more complex types requires us to recurse into each subtype
                return_dict = {
                    "anyOf": [_parse_type_hint(t) for t in get_args(hint) if t not in (type(None), ...)],
                }
                if len(return_dict["anyOf"]) == 1:
                    return_dict = return_dict["anyOf"][0]
            if type(None) in get_args(hint):
                return_dict["nullable"] = True
            return return_dict
        elif origin is list:
            if not get_args(hint):
                return {"type": "array"}
            if all(t in BASIC_TYPES for t in get_args(hint)):
                # Similarly to unions, a list of basic types can be expressed as a list in the schema
                items = {"type": [_get_json_schema_type(t)["type"] for t in get_args(hint) if t != type(None)]}
                if len(items["type"]) == 1:
                    items["type"] = items["type"][0]
            else:
                # And a list of more complex types requires us to recurse into each subtype again
                items = {"anyOf": [_parse_type_hint(t) for t in get_args(hint) if t not in (type(None), ...)]}
                if len(items["anyOf"]) == 1:
                    items = items["anyOf"][0]
            return_dict = {"type": "array", "items": items}
            if type(None) in get_args(hint):
                return_dict["nullable"] = True
            return return_dict
        elif origin is tuple:
            if not get_args(hint):
                return {"type": "array"}
            if len(get_args(hint)) == 1:
                raise ValueError(
                    "Tuple type hints should only be used when the argument has a fixed length and each "
                    f"element has a specific type. The hint {hint} indicates a Tuple of length 1. "
                    "This should be replaced with an unwrapped type hint instead like "
                    f"{get_args(hint)[0]}. Alternatively, if the "
                    "function can actually take a tuple with multiple elements, please either indicate "
                    f"each element type (e.g. Tuple[{get_args(hint)[0]}, {get_args(hint)[0]}]), "
                    f"or if the input can be variable length, use List[{get_args(hint)[0]}] instead."
                )
            if ... in get_args(hint):
                raise ValueError(
                    "'...' is not supported in Tuple type hints. Use List[] types for variable-length"
                    " inputs instead."
                )
            return {"type": "array", "prefixItems": [_parse_type_hint(t) for t in get_args(hint)]}
        elif origin is dict:
            # The JSON equivalent to a dict is 'object', which mandates that all keys are strings
            # However, we can specify the type of the dict values with "additionalProperties"
            out = {"type": "object"}
            if len(get_args(hint)) == 2:
                out["additionalProperties"] = _parse_type_hint(get_args(hint)[1])
            return out
        else:
            raise ValueError("Couldn't parse this type hint, likely due to a custom class or object: ", hint)
    else:
        return _get_json_schema_type(hint)


def _get_json_schema_type(param_type):
    if param_type == int:
        return {"type": "integer"}
    elif param_type == float:
        return {"type": "number"}
    elif param_type == str:
        return {"type": "string"}
    elif param_type == bool:
        return {"type": "boolean"}
    elif param_type == Any:
        return {}
    else:
        return {"type": "object"}
