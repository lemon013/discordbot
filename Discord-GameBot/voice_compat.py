"""discord.py 2.7.1 voice flow with davey 0.2 key transitions.

The 0.2 migration requires prepare_transition after commit/welcome, and
execute_transition on the server's execute event. No keys or payloads are logged.
Reference: https://github.com/Snazzah/davey/wiki/Migrating-to-0.2.0
"""
import logging
import struct

import davey
import discord
from discord.gateway import DiscordVoiceWebSocket
from discord.utils import MISSING
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

log = logging.getLogger(__name__)


class CompatibleVoiceWebSocket(DiscordVoiceWebSocket):
    async def received_binary_message(self, msg):
        state = self._connection
        session = state.dave_session
        op = msg[2] if len(msg) >= 3 else None
        if session is None or op not in (self.MLS_ANNOUNCE_COMMIT_TRANSITION, self.MLS_WELCOME):
            return await super().received_binary_message(msg)
        if len(msg) < 5:
            raise discord.ClientException('Truncated DAVE transition message')
        self.seq_ack = struct.unpack_from('>H', msg, 0)[0]
        transition_id = struct.unpack_from('>H', msg, 3)[0]
        try:
            if op == self.MLS_ANNOUNCE_COMMIT_TRANSITION:
                # 0.2 returns False for a legitimately ignored commit.
                if not session.process_commit(msg[5:]):
                    return
            else:
                session.process_welcome(msg[5:])
            session.prepare_transition(transition_id, state.dave_protocol_version)
            log.info('DAVE key transition prepared (id=%s, protocol=%s)', transition_id, state.dave_protocol_version)
        except Exception:
            log.warning('DAVE commit/welcome rejected; requesting new key package (id=%s)', transition_id)
            await state._recover_from_invalid_commit(transition_id)
            return
        if transition_id != 0:
            state.dave_pending_transitions[transition_id] = state.dave_protocol_version
            await self.send_transition_ready(transition_id)

    async def received_message(self, msg):
        if msg['op'] != self.DAVE_PREPARE_TRANSITION or self._connection.dave_session is None:
            return await super().received_message(msg)
        self.seq_ack = msg.get('seq', self.seq_ack)
        state, data = self._connection, msg['d']
        transition_id, protocol = data['transition_id'], data['protocol_version']
        state.dave_session.prepare_transition(transition_id, protocol)
        state.dave_pending_transitions[transition_id] = protocol
        if transition_id == 0:
            # Davey applies transition zero during prepare_transition itself.
            await state._execute_transition(transition_id)
        else:
            await self.send_transition_ready(transition_id)
        await self._hook(self, msg)


class CompatibleVoiceState(VoiceConnectionState):
    @property
    def can_encrypt(self):
        return (self.dave_protocol_version != 0 and self.dave_session is not None
                and self.dave_session.can_encrypt)

    async def _connect_websocket(self, resume):
        seq_ack = self.ws.seq_ack if self.ws is not MISSING else -1
        ws = await CompatibleVoiceWebSocket.from_connection_state(self, resume=resume, hook=self.hook, seq_ack=seq_ack)
        self.state = ConnectionFlowState.websocket_connected
        return ws

    async def _execute_transition(self, transition_id):
        if transition_id not in self.dave_pending_transitions:
            log.warning('Ignored unexpected DAVE transition (id=%s)', transition_id)
            return
        protocol = self.dave_pending_transitions[transition_id]
        if self.dave_session is not None and transition_id != 0:
            self.dave_session.execute_transition(transition_id)
        old_protocol = self.dave_protocol_version
        self.dave_protocol_version = self.dave_pending_transitions.pop(transition_id)
        if old_protocol != protocol and protocol == 0:
            self.dave_downgraded = True
        elif transition_id > 0 and self.dave_downgraded:
            self.dave_downgraded = False
            if self.dave_session:
                self.dave_session.set_passthrough_mode(False, 10)
        log.info('DAVE key transition activated (id=%s, protocol=%s, can_encrypt=%s)',
                 transition_id, protocol, self.can_encrypt)


class CompatibleVoiceClient(discord.VoiceClient):
    def create_connection_state(self):
        if hasattr(davey.DaveSession, 'prepare_transition'):
            return CompatibleVoiceState(self)
        return super().create_connection_state()
