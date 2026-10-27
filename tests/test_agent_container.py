"""Regressions for isolated agent container recovery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_container import ContainerOpenCode


class AgentContainerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.client = ContainerOpenCode(Path(self.tempdir.name) / 'runtime')

    def test_lowercase_docker_missing_object_is_reported_as_missing_container(self):
        with patch.object(self.client, '_make_proxy'), patch.object(
            self.client, '_docker', side_effect=RuntimeError(
                'Docker agent runtime failed: error: no such object: hearth-agent-fixture')):
            with self.assertRaisesRegex(RuntimeError, 'isolated agent container is missing'):
                self.client._endpoint('hearth-agent-fixture', {'model_proxy': {}})

    def test_other_docker_inspect_errors_are_preserved(self):
        with patch.object(self.client, '_make_proxy'), patch.object(
            self.client, '_docker', side_effect=RuntimeError('Docker daemon is unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'Docker daemon is unavailable'):
                self.client._endpoint('hearth-agent-fixture', {'model_proxy': {}})


if __name__ == '__main__':
    unittest.main()
