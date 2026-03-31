"""Parse Atlassian Document Format (ADF) to plain text."""
import json
from typing import Union, Dict, List


def adf_to_text(adf: Union[str, Dict]) -> str:
    """
    Convert Atlassian Document Format to plain text.

    Args:
        adf: Either an ADF dict or a string (which might be ADF JSON or plain text)

    Returns:
        Plain text representation
    """
    # If it's a string, try to parse as JSON
    if isinstance(adf, str):
        try:
            adf = json.loads(adf)
        except (json.JSONDecodeError, TypeError):
            # Not JSON, return as-is
            return adf

    # If it's not a dict, return string representation
    if not isinstance(adf, dict):
        return str(adf)

    # If it's not ADF format, return string representation
    if 'type' not in adf:
        return str(adf)

    # Parse ADF
    return _parse_adf_node(adf)


def _parse_adf_node(node: Dict) -> str:
    """Recursively parse an ADF node."""
    node_type = node.get('type', '')

    if node_type == 'doc':
        content = node.get('content', [])
        return '\n\n'.join(_parse_adf_node(child) for child in content)

    elif node_type == 'paragraph':
        content = node.get('content', [])
        return ''.join(_parse_adf_node(child) for child in content)

    elif node_type == 'text':
        text = node.get('text', '')
        return text

    elif node_type == 'hardBreak':
        return '\n'

    elif node_type in ['heading', 'blockquote']:
        content = node.get('content', [])
        text = ''.join(_parse_adf_node(child) for child in content)
        if node_type == 'heading':
            level = node.get('attrs', {}).get('level', 1)
            return f"{'#' * level} {text}"
        return f"> {text}"

    elif node_type in ['bulletList', 'orderedList']:
        content = node.get('content', [])
        items = [_parse_adf_node(child) for child in content]
        if node_type == 'bulletList':
            return '\n'.join(f"- {item}" for item in items)
        else:
            return '\n'.join(f"{i+1}. {item}" for i, item in enumerate(items))

    elif node_type == 'listItem':
        content = node.get('content', [])
        return '\n'.join(_parse_adf_node(child) for child in content)

    elif node_type == 'codeBlock':
        content = node.get('content', [])
        code = ''.join(_parse_adf_node(child) for child in content)
        return f"```\n{code}\n```"

    elif node_type == 'inlineCard':
        url = node.get('attrs', {}).get('url', '')
        return url

    elif node_type == 'mention':
        text = node.get('attrs', {}).get('text', '@unknown')
        return text

    # For unknown types, try to extract content
    elif 'content' in node:
        content = node.get('content', [])
        return ''.join(_parse_adf_node(child) for child in content)

    # Fallback
    return ''
