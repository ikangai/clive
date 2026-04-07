"""Tests for script extraction from LLM responses."""
from executor import _extract_script
import pytest


def test_extract_fenced_bash():
    text = '''Here's the script:

```bash
#!/bin/bash
set -e
echo "hello"
```

This should work.'''
    script = _extract_script(text)
    assert script.startswith("#!/bin/bash")
    assert 'echo "hello"' in script


def test_extract_fenced_sh():
    text = '''```sh
#!/bin/bash
ls -la
```'''
    script = _extract_script(text)
    assert "ls -la" in script


def test_extract_unfenced():
    text = '''#!/bin/bash
set -e
echo "test"'''
    script = _extract_script(text)
    assert script.startswith("#!/bin/bash")


def test_extract_no_script_raises():
    with pytest.raises(ValueError):
        _extract_script("Just some text without any script")


def test_extract_strips_whitespace():
    text = '''```bash

#!/bin/bash
echo "hi"

```'''
    script = _extract_script(text)
    assert script.startswith("#!/bin/bash")
    assert not script.startswith("\n")
