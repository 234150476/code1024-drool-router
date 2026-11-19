# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Any supplier reachable over a supported wire protocol must work unmodified.

This is what makes "supports any model" true rather than a marketing claim: a
new platform needs config, not a code change.
"""
import json
import unittest
import uuid
from unittest.mock import Mock, patch

from detector import config
from detector.upstream import ProbeError, build_request, client_info

MOONSHOT = {'enabled': True, 'label': 'Kimi', 'model': 'kimi-k3', 'effort': 'max',
            'group_ids': [], 'group_names': [], 'exclude_names': []}


def account(platform='moonshot', **creds):
    return {'id': 1, 'name': 'relay', 'platform': platform, 'type': 'apikey',
            'status': 'active', 'credentials': {'api_key': 'offline-key', **creds}}


def with_platform(name, spec):
    platforms = dict(config.CONFIG['platforms'])
    platforms[name] = spec
    return patch.dict(config.CONFIG, {'platforms': platforms})


class ProtocolResolutionTests(unittest.TestCase):
    def test_known_platforms_keep_their_native_protocol(self):
        self.assertEqual(config.protocol_for('openai'), 'openai_responses')
        self.assertEqual(config.protocol_for('anthropic'), 'anthropic_messages')
        self.assertEqual(config.protocol_for('gemini'), 'gemini_generate')
        self.assertEqual(config.protocol_for('grok'), 'xai_responses')

    def test_an_unknown_platform_defaults_to_openai_compatible_chat(self):
        for platform in ('moonshot', 'deepseek', 'qwen', 'openrouter', 'siliconflow', 'anything'):
            with self.subTest(platform=platform):
                self.assertEqual(config.protocol_for(platform), 'openai_chat')

    def test_an_explicit_protocol_overrides_the_implied_one(self):
        spec = {**MOONSHOT, 'protocol': 'openai_responses'}
        with with_platform('moonshot', spec):
            self.assertEqual(config.protocol_for('moonshot'), 'openai_responses')

    def test_an_unknown_protocol_name_fails_loudly_at_startup(self):
        spec = {**MOONSHOT, 'protocol': 'telepathy'}
        with with_platform('moonshot', spec):
            with self.assertRaises(SystemExit):
                config.protocol_for('moonshot')


class GenericAdapterTests(unittest.TestCase):
    def test_openai_apikey_responses_matches_gateway_account_test_client_profile(self):
        with patch.dict(config.CONFIG, {'upstream': {'codex_version': '0.169.1'}}):
            url, headers, payload = build_request(
                account('openai', base_url='https://coding-relay.test'), 'drawing',
                prompt='inspection question', instructions='inspection contract')
        self.assertEqual(url, 'https://coding-relay.test/v1/responses')
        self.assertEqual(headers['Authorization'], 'Bearer offline-key')
        self.assertEqual(headers['Originator'], 'codex_cli_rs')
        self.assertEqual(headers['Version'], '0.169.1')
        self.assertTrue(headers['User-Agent'].startswith('codex_cli_rs/0.169.1 '))
        self.assertEqual(headers['OpenAI-Beta'], 'responses=experimental')
        self.assertEqual(str(uuid.UUID(headers['X-Codex-Window-ID'])), headers['X-Codex-Window-ID'])
        self.assertEqual(payload['input'][0]['content'][0]['text'], 'inspection question')
        self.assertIn('inspection contract', payload['instructions'])

    def test_openai_apikey_probes_have_independent_window_and_request_ids(self):
        _, first, _ = build_request(account('openai'), 'drawing')
        _, second, _ = build_request(account('openai'), 'drawing')
        self.assertNotEqual(first['X-Codex-Window-ID'], second['X-Codex-Window-ID'])
        self.assertNotEqual(first['X-Client-Request-Id'], second['X-Client-Request-Id'])
        self.assertEqual(first['Cache-Control'], 'no-cache, no-store')

    def test_other_responses_platforms_keep_their_existing_client_profile(self):
        with with_platform('moonshot', {**MOONSHOT, 'protocol': 'openai_responses'}):
            url, headers, payload = build_request(
                account(base_url='https://relay.test/v1'), 'candy')
        self.assertEqual(url, 'https://relay.test/v1/responses')
        self.assertEqual(payload['model'], 'kimi-k3')
        self.assertNotIn('Originator', headers)
        self.assertNotIn('Version', headers)
        self.assertNotIn('X-Codex-Window-ID', headers)

    def test_oauth_identity_uses_configured_client_version_and_matching_originator(self):
        a = {**account('openai'), 'type': 'oauth', 'credentials': {'access_token': 'fixture-token',
             'chatgpt_account_id': 'fixture-workspace'}}
        with patch.dict(config.CONFIG, {'upstream': {'codex_version': '0.169.1'}}):
            url, headers, payload = build_request(a, 'candy')
            self.assertEqual(headers['Version'], '0.169.1')
            self.assertTrue(headers['User-Agent'].startswith('codex_cli_rs/0.169.1 '))
            self.assertEqual(headers['Originator'], 'codex_cli_rs')
            self.assertEqual(client_info('openai')['codex_protocol_version'], '0.169.1')
            self.assertEqual(headers['ChatGPT-Account-Id'], 'fixture-workspace')
        with patch.dict(config.CONFIG, {'upstream': {'codex_version': 'bad\r\nversion'}}):
            with self.assertRaises(ProbeError):
                build_request(a, 'candy')

    def test_a_new_platform_needs_config_only(self):
        with with_platform('moonshot', MOONSHOT):
            url, headers, payload = build_request(
                account(base_url='https://api.moonshot.cn/v1'), 'candy')
        self.assertEqual(url, 'https://api.moonshot.cn/v1/chat/completions')
        self.assertEqual(headers['Authorization'], 'Bearer offline-key')
        self.assertEqual(payload['model'], 'kimi-k3')
        self.assertTrue(payload['stream'])
        self.assertEqual(payload['reasoning_effort'], 'max')
        roles = [message['role'] for message in payload['messages']]
        self.assertEqual(roles, ['system', 'user'])

    def test_the_system_message_carries_the_grading_contract(self):
        with with_platform('moonshot', MOONSHOT):
            _, _, payload = build_request(account(base_url='https://api.moonshot.cn/v1'), 'candy')
        instructions = payload['messages'][0]['content']
        self.assertIn('独立请求标识', instructions)
        self.assertIn('最终答案', instructions)

    def test_every_request_gets_a_unique_id_and_no_cache(self):
        with with_platform('moonshot', MOONSHOT):
            _, first, _ = build_request(account(base_url='https://api.moonshot.cn/v1'), 'candy')
            _, second, _ = build_request(account(base_url='https://api.moonshot.cn/v1'), 'candy')
        self.assertNotEqual(first['X-Client-Request-Id'], second['X-Client-Request-Id'])
        self.assertEqual(first['Cache-Control'], 'no-cache, no-store')

    def test_a_supplier_without_a_base_url_falls_back_to_official_openai(self):
        with with_platform('moonshot', MOONSHOT):
            url, _, _ = build_request(account(), 'candy')
        self.assertEqual(url, 'https://api.openai.com/v1/chat/completions')

    def test_a_trailing_v1_in_the_base_url_is_not_duplicated(self):
        with with_platform('moonshot', MOONSHOT):
            url, _, _ = build_request(account(base_url='https://relay.test/v1'), 'candy')
        self.assertEqual(url, 'https://relay.test/v1/chat/completions')

    def test_the_prompt_is_not_replaced_by_the_adapter(self):
        with with_platform('moonshot', MOONSHOT):
            _, _, payload = build_request(account(base_url='https://relay.test/v1'), 'drawing')
        self.assertIn('SVG', payload['messages'][1]['content'])
        self.assertIn('火烈鸟', payload['messages'][1]['content'])

    def test_a_custom_question_override_reaches_the_generic_adapter(self):
        with with_platform('moonshot', MOONSHOT):
            _, _, payload = build_request(account(base_url='https://relay.test/v1'), 'candy',
                                          prompt='private question', instructions='private contract')
        self.assertEqual(payload['messages'][1]['content'], 'private question')
        self.assertIn('private contract', payload['messages'][0]['content'])

    def test_credentials_are_required_and_named_in_the_error(self):
        with with_platform('moonshot', MOONSHOT):
            with self.assertRaises(ProbeError) as exc:
                build_request({'platform': 'moonshot', 'type': 'apikey', 'status': 'active',
                               'credentials': {}}, 'candy')
        self.assertEqual(exc.exception.code, 'CREDENTIAL_UNAVAILABLE')

    def test_an_inactive_non_openai_account_is_never_called(self):
        with with_platform('moonshot', MOONSHOT):
            with self.assertRaises(ProbeError) as exc:
                build_request({**account(base_url='https://relay.test/v1'), 'status': 'inactive'}, 'candy')
        self.assertEqual(exc.exception.code, 'ACCOUNT_INACTIVE')

    def test_client_info_reports_the_protocol_for_audit(self):
        with with_platform('moonshot', MOONSHOT):
            self.assertEqual(client_info('moonshot')['protocol'], 'openai-chat-completions')
        self.assertEqual(client_info('gemini')['protocol'], 'gemini-generateContent-v1beta')

    def test_an_account_flag_downgrades_openai_to_chat_completions(self):
        _, headers, payload = build_request(
            {**account('openai'), 'extra': {'openai_responses_supported': False}}, 'candy')
        self.assertIn('messages', payload)
        self.assertNotIn('input', payload)
        self.assertNotIn('Originator', headers)
        self.assertNotIn('X-Codex-Window-ID', headers)


class ScopeAppliesToGenericPlatformsTests(unittest.TestCase):
    def test_a_configured_generic_platform_is_probed(self):
        from detector.upstream import in_evaluation_scope
        with with_platform('moonshot', MOONSHOT):
            self.assertTrue(in_evaluation_scope({'platform': 'moonshot', 'name': 'relay'}))

    def test_a_disabled_generic_platform_is_not_probed(self):
        from detector.upstream import in_evaluation_scope
        with with_platform('moonshot', {**MOONSHOT, 'enabled': False}):
            self.assertFalse(in_evaluation_scope({'platform': 'moonshot', 'name': 'relay'}))

    def test_an_unconfigured_platform_is_not_probed(self):
        from detector.upstream import in_evaluation_scope
        self.assertFalse(in_evaluation_scope({'platform': 'never-heard-of-it', 'name': 'relay'}))


if __name__ == '__main__':
    unittest.main()
