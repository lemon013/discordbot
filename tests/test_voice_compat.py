import struct
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from voice_compat import CompatibleVoiceClient, CompatibleVoiceState, CompatibleVoiceWebSocket


class Session:
    """Model 0.2 semantics: processing alone never installs an encryption key."""
    def __init__(self):
        self.ready = self.can_encrypt = False
        self.pending = {}
        self.process_commit = Mock(return_value=True)
        self.process_welcome = Mock()
        self.set_passthrough_mode = Mock()
        self.activated = []

    def prepare_transition(self, transition_id, protocol):
        self.pending[transition_id] = protocol
        if transition_id == 0:
            self.ready = self.can_encrypt = protocol > 0

    def execute_transition(self, transition_id):
        protocol = self.pending.pop(transition_id)
        self.ready = self.can_encrypt = protocol > 0
        self.activated.append(transition_id)


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = Session()
        self.state = object.__new__(CompatibleVoiceState)
        self.state.dave_session = self.session
        self.state.dave_protocol_version = 1
        self.state.dave_downgraded = False
        self.state.dave_pending_transitions = {}
        self.state._recover_from_invalid_commit = AsyncMock()
        self.socket = object.__new__(CompatibleVoiceWebSocket)
        self.socket._connection = self.state
        self.socket.seq_ack = -1
        self.socket.send_transition_ready = AsyncMock()
        self.socket._hook = AsyncMock()

    async def binary(self, opcode, transition_id):
        await self.socket.received_binary_message(struct.pack('>HBH', 7, opcode, transition_id) + b'fixture')

    async def test_initial_welcome_prepares_zero_and_activates_keys(self):
        await self.binary(self.socket.MLS_WELCOME, 0)
        self.session.process_welcome.assert_called_once_with(b'fixture')
        self.assertTrue(self.state.can_encrypt)
        self.assertEqual(self.socket.seq_ack, 7)
        self.socket.send_transition_ready.assert_not_awaited()

    async def test_commit_keys_wait_for_server_execute(self):
        await self.binary(self.socket.MLS_ANNOUNCE_COMMIT_TRANSITION, 9)
        self.assertFalse(self.state.can_encrypt)
        self.socket.send_transition_ready.assert_awaited_once_with(9)
        await self.socket.received_message({'op':self.socket.DAVE_EXECUTE_TRANSITION,
                                           'd':{'transition_id':9}, 'seq':8})
        self.assertTrue(self.state.can_encrypt)
        self.assertEqual(self.session.activated, [9])
        self.assertFalse(self.state.dave_pending_transitions)

    async def test_welcome_nonzero_keys_wait_for_server_execute(self):
        await self.binary(self.socket.MLS_WELCOME, 12)
        self.assertFalse(self.state.can_encrypt)
        await self.state._execute_transition(12)
        self.assertTrue(self.state.can_encrypt)

    async def test_ignored_commit_cannot_prepare_or_activate_keys(self):
        self.session.process_commit.return_value = False
        await self.binary(self.socket.MLS_ANNOUNCE_COMMIT_TRANSITION, 9)
        self.assertFalse(self.session.pending)
        self.assertFalse(self.state.can_encrypt)
        self.socket.send_transition_ready.assert_not_awaited()

    async def test_invalid_welcome_recovers_without_installing_keys(self):
        self.session.process_welcome.side_effect = ValueError('invalid signature')
        await self.binary(self.socket.MLS_WELCOME, 2)
        self.state._recover_from_invalid_commit.assert_awaited_once_with(2)
        self.assertFalse(self.state.can_encrypt)
        self.assertFalse(self.session.pending)

    async def test_server_prepares_zero_without_double_execution(self):
        await self.socket.received_message({'op':self.socket.DAVE_PREPARE_TRANSITION,
            'd':{'transition_id':0,'protocol_version':1},'seq':9})
        self.assertTrue(self.state.can_encrypt)
        self.assertEqual(self.session.activated, [])
        self.assertFalse(self.state.dave_pending_transitions)

    async def test_negotiated_downgrade_and_upgrade_follow_server_transitions(self):
        await self.socket.received_message({'op':self.socket.DAVE_PREPARE_TRANSITION,
            'd':{'transition_id':3,'protocol_version':0}})
        self.assertEqual(self.state.dave_protocol_version, 1)
        await self.state._execute_transition(3)
        self.assertEqual(self.state.dave_protocol_version, 0)
        self.assertFalse(self.state.can_encrypt)
        self.assertTrue(self.state.dave_downgraded)
        await self.socket.received_message({'op':self.socket.DAVE_PREPARE_TRANSITION,
            'd':{'transition_id':4,'protocol_version':1}})
        await self.state._execute_transition(4)
        self.assertTrue(self.state.can_encrypt)
        self.session.set_passthrough_mode.assert_called_once_with(False, 10)

    async def test_unknown_execute_cannot_activate_keys(self):
        await self.state._execute_transition(99)
        self.assertFalse(self.state.can_encrypt)
        self.assertFalse(self.session.activated)

    def test_ready_without_usable_key_does_not_pass_encrypt_gate(self):
        self.session.ready = True
        self.assertFalse(self.state.can_encrypt)

    def test_client_uses_compatible_connection_state(self):
        with patch('voice_compat.CompatibleVoiceState', return_value='compatible') as create:
            self.assertEqual(CompatibleVoiceClient.create_connection_state(SimpleNamespace()), 'compatible')
            create.assert_called_once()


if __name__ == '__main__':
    unittest.main()
