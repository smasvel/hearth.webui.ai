"""Agent skill library: storage, assignment selection, container install, and imports."""
import io
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import agent_store
import database
import users_store


def temporary_schema(test):
    scope = database.temporary_schema()
    scope.__enter__()
    test.addCleanup(scope.__exit__, None, None, None)


def make_owner():
    return users_store.create_user('skill-test-' + uuid.uuid4().hex[:12], 'password', 'admin')['id']


class SkillFolderTests(unittest.TestCase):
    def test_folder_slugs_and_rejects_unusable_names(self):
        self.assertEqual(agent_store.skill_folder('Code Review Checklist'), 'code-review-checklist')
        self.assertEqual(agent_store.skill_folder('  deploy--steps '), 'deploy-steps')
        with self.assertRaises(ValueError):
            agent_store.skill_folder('---')

    def test_compose_markdown_adds_frontmatter_unless_present(self):
        skill = {'name': 'Review', 'description': 'Check code', 'body': 'Do the thing.'}
        markdown = agent_store.compose_skill_markdown(skill)
        self.assertTrue(markdown.startswith('---\nname: "Review"'))
        self.assertIn('description: "Check code"', markdown)
        self.assertIn('Do the thing.', markdown)
        existing = '---\nname: "Custom"\n---\nBody text.'
        self.assertEqual(agent_store.compose_skill_markdown({'body': existing}), existing)


class SkillStoreTests(unittest.TestCase):
    def setUp(self):
        temporary_schema(self)
        self.owner = make_owner()

    def test_save_list_get_delete_roundtrip(self):
        skill = agent_store.save_skill({'owner': self.owner, 'name': 'Deploy helper',
                                        'description': 'Deployment steps', 'body': 'Run npm build.'})
        listed = agent_store.list_skills(self.owner)
        self.assertEqual([item['name'] for item in listed], ['Deploy helper'])
        self.assertEqual(agent_store.get_skill(self.owner, skill['id'])['body'], 'Run npm build.')
        self.assertEqual(agent_store.get_skill_by_name(self.owner, 'Deploy helper')['id'], skill['id'])
        self.assertTrue(agent_store.delete_skill(self.owner, skill['id']))
        self.assertEqual(agent_store.list_skills(self.owner), [])
        self.assertFalse(agent_store.delete_skill(self.owner, skill['id']))

    def test_save_updates_same_skill_for_same_name(self):
        first = agent_store.save_skill({'owner': self.owner, 'name': 'Notes', 'body': 'One.'})
        second = agent_store.save_skill({'owner': self.owner, 'name': 'Notes', 'body': 'Two.'})
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(agent_store.list_skills(self.owner)[0]['body'], 'Two.')

    def test_body_validation(self):
        with self.assertRaises(ValueError):
            agent_store.save_skill({'owner': self.owner, 'name': 'Empty', 'body': '   '})
        with self.assertRaises(ValueError):
            agent_store.save_skill({'owner': self.owner, 'name': 'Huge', 'body': 'x' * 200_001})

    def test_get_skills_by_names_preserves_order_and_skips_unknown(self):
        agent_store.save_skill({'owner': self.owner, 'name': 'A', 'body': 'a'})
        agent_store.save_skill({'owner': self.owner, 'name': 'B', 'body': 'b'})
        picked = agent_store.get_skills_by_names(self.owner, ['B', 'Missing', 'A'])
        self.assertEqual([skill['name'] for skill in picked], ['B', 'A'])
        self.assertEqual(agent_store.get_skills_by_names(self.owner, []), [])


class AssignmentSkillTests(unittest.TestCase):
    def setUp(self):
        temporary_schema(self)
        self.owner = make_owner()
        agent_store.save_skill({'owner': self.owner, 'name': 'Deploy helper', 'body': 'Steps.'})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = Mock()
        self.client.models.return_value = [{'providerID': 'lmstudio', 'id': 'qwen', 'name': 'Qwen'}]
        import agent_tasks
        from contextlib import ExitStack
        self.stack = ExitStack()
        self.stack.enter_context(patch('agent_tasks.Path.home', return_value=Path(self.tmp.name)))
        self.manager = agent_tasks.Manager(Path(self.tmp.name) / 'tasks', self.client)
        self.addCleanup(self.stack.close)
        self.values = {'title': 'Build it', 'goal': 'Build the feature',
                       'model': {'providerID': 'lmstudio', 'id': 'qwen'}, 'assignee': 'Worker'}

    def test_create_assignment_with_selected_skills(self):
        job = self.manager.create({**self.values, 'skills': ['Deploy helper']}, self.owner)
        self.assertEqual(job['skills'], ['Deploy helper'])

    def test_unknown_skill_is_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.create({**self.values, 'skills': ['Not saved']}, self.owner)

    def test_skills_must_be_names(self):
        with self.assertRaises(ValueError):
            self.manager.create({**self.values, 'skills': ['Deploy helper', 5]}, self.owner)

    def test_update_assignment_replaces_skills(self):
        key = self.manager.create({**self.values, 'skills': ['Deploy helper']}, self.owner)['id']
        self.manager.jobs[key]['status'] = 'completed'
        self.manager.update_assignment(key, {**self.values, 'skills': []})
        self.assertEqual(self.manager.get(key)['skills'], [])
        self.manager.update_assignment(key, {**self.values, 'skills': ['Deploy helper']})
        self.assertEqual(self.manager.get(key)['skills'], ['Deploy helper'])


class SnapshotBytesIO(io.BytesIO):
    def __init__(self, process):
        super().__init__()
        self.process = process

    def close(self):
        self.process.captured = self.getvalue()
        super().close()


class FakeTarProcess:
    def __init__(self, args):
        self.args = args
        self.stdin = SnapshotBytesIO(self)
        self.stderr = io.BytesIO()
        self.captured = None
    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class ContainerSkillTests(unittest.TestCase):
    def setUp(self):
        temporary_schema(self)
        self.owner = make_owner()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import agent_container
        self.agent_container = agent_container
        self.container = agent_container.ContainerOpenCode(Path(self.tmp.name) / 'runtime')
        self.container._docker = Mock()

    def skill_job(self, names):
        return {'owner': self.owner, 'skills': names}

    def test_sync_installs_skill_markdown_tar(self):
        skill = agent_store.save_skill({'owner': self.owner, 'name': 'Deploy Helper',
                                        'description': 'Steps', 'body': 'Run the deploy.'})
        processes = []
        with patch.object(self.agent_container.subprocess, 'Popen',
                          side_effect=lambda args, **kwargs: processes.append(FakeTarProcess(args)) or processes[-1]):
            self.container._sync_skills_to_container('hearth-agent-x', self.skill_job(['Deploy Helper']))
        self.assertEqual(processes[0].args[:4], ['docker', 'exec', '-i', 'hearth-agent-x'])
        self.assertIn("rm -rf /home/node/.config/opencode/skill", self.container._docker.call_args.args[4])
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(processes[0].captured)) as tar:
            names = tar.getnames()
            deployed = tar.extractfile('deploy-helper/SKILL.md').read().decode()
        self.assertEqual(names, ['deploy-helper', 'deploy-helper/SKILL.md'])
        self.assertIn('name: "Deploy Helper"', deployed)
        self.assertIn('Run the deploy.', deployed)

    def test_sync_without_skills_skips_docker_entirely(self):
        processes = []
        with patch.object(self.agent_container.subprocess, 'Popen',
                          side_effect=lambda args, **kwargs: processes.append(FakeTarProcess(args)) or processes[-1]):
            self.container._sync_skills_to_container('hearth-agent-x', self.skill_job([]))
        self.container._docker.assert_not_called()
        self.assertEqual(processes, [])

    def test_agent_config_mentions_selected_skills(self):
        config = self.container._agent_config({}, 'lmstudio', 'qwen',
                                              {'options': {}, 'models': {'qwen': {}}}, skills=['Deploy Helper'])
        system = config['agents']['hearth-worker']['system']
        self.assertIn('Deploy Helper', system)
        self.assertNotIn('SKILL.md instructions', self.agent_container.SYSTEM)

    def test_skill_change_requires_fresh_container_config(self):
        import copy
        base = self.container._agent_config({}, 'lmstudio', 'qwen',
                                            {'options': {}, 'models': {'qwen': {}}}, skills=['A'])
        changed = self.container._agent_config({}, 'lmstudio', 'qwen',
                                               {'options': {}, 'models': {'qwen': {}}}, skills=['A', 'B'])
        self.assertNotEqual(base, changed)


class SkillImportHelperTests(unittest.TestCase):
    def setUp(self):
        temporary_schema(self)
        self.owner = make_owner()
        import app
        self.app = app

    def test_parse_frontmatter(self):
        meta, body = self.app._parse_skill_frontmatter('---\nname: "Review"\ndescription: Checks\n---\nDo work.')
        self.assertEqual(meta['name'], 'Review')
        self.assertEqual(meta['description'], 'Checks')
        self.assertEqual(body, 'Do work.')
        meta, body = self.app._parse_skill_frontmatter('Just instructions.')
        self.assertEqual(meta, {})
        self.assertEqual(body, 'Just instructions.')

    def test_import_uses_frontmatter_name_and_uniquifies(self):
        skill = self.app._import_skill(self.owner, 'ignored', '', '---\nname: Review\ndescription: d\n---\nSteps.')
        self.assertEqual(skill['name'], 'Review')
        self.assertEqual(skill['description'], 'd')
        self.assertEqual(skill['body'], 'Steps.')
        second = self.app._import_skill(self.owner, 'ignored', '', '---\nname: Review\n---\nMore.')
        self.assertEqual(second['name'], 'Review (2)')
        self.assertEqual(len(agent_store.list_skills(self.owner)), 2)

    def test_skills_from_zip_extracts_each_skill_md(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('review/SKILL.md', '---\nname: Review\n---\nCheck things.')
            z.writestr('deploy/SKILL.md', 'Plain deploy body.')
            z.writestr('other/notes.txt', 'ignored')
        archive.seek(0)
        created = self.app._skills_from_zip(self.owner, archive.read(), 'fallback')
        self.assertEqual(sorted(skill['name'] for skill in created), ['Review', 'deploy'])
        by_name = {skill['name']: skill for skill in created}
        self.assertEqual(by_name['Review']['body'], 'Check things.')
        self.assertEqual(by_name['deploy']['body'], 'Plain deploy body.')

    def test_skills_from_zip_rejects_missing_skill_md(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('readme.txt', 'nope')
        archive.seek(0)
        with self.assertRaises(ValueError):
            self.app._skills_from_zip(self.owner, archive.read(), 'fallback')


if __name__ == '__main__':
    unittest.main()
