import collections
import copyreg
import json
import logging
import pprint
import re
import sys
from functools import lru_cache
from types import MethodType
from typing import BinaryIO

from errbot.backends.base import AWAY
from errbot.backends.base import Card
from errbot.backends.base import Identifier
from errbot.backends.base import Message
from errbot.backends.base import ONLINE
from errbot.backends.base import Presence
from errbot.backends.base import Reaction
from errbot.backends.base import REACTION_ADDED
from errbot.backends.base import REACTION_REMOVED
from errbot.backends.base import RoomDoesNotExistError
from errbot.backends.base import RoomOccupant
from errbot.backends.base import Stream
from errbot.backends.base import UserDoesNotExistError
from errbot.backends.slack import COLORS
from errbot.backends.slack import SLACK_CLIENT_CHANNEL_HYPERLINK
from errbot.backends.slack import slack_markdown_converter
from errbot.backends.slack import SlackAPIResponseError
from errbot.backends.slack import SlackBot
from errbot.backends.slack import SlackPerson
from errbot.backends.slack import SlackRoom
from errbot.backends.slack import SlackRoomBot
from errbot.backends.slack import SlackRoomOccupant
from errbot.core import ErrBot
from errbot.utils import split_string_after

log = logging.getLogger(__name__)

try:
    from slackclient import SlackClient
except ImportError:
    log.exception("Could not start the Slack back-end")
    log.fatal(
        "You need to install the slackclient support in order to use the Slack backend.\n"
        "You can do `pip install errbot[slack]` to install it"
    )
    sys.exit(1)


def _make_handler_function(callback_name):
    def _function(self, message):  # noqa: F821
        self._dispatch_to_plugins(f"{callback_name}", message)  # noqa: F821

    return _function


class SlackExtendedBackend(ErrBot):

    room_types = "public_channel,private_channel"

    @staticmethod
    def _unpickle_identifier(identifier_str):
        return SlackExtendedBackend.__build_identifier(identifier_str)

    @staticmethod
    def _pickle_identifier(identifier):
        return SlackExtendedBackend._unpickle_identifier, (str(identifier),)

    def _register_identifiers_pickling(self):
        """
        Register identifiers pickling.

        As Slack needs live objects in its identifiers, we need to override their pickling behavior.
        But for the unpickling to work we need to use bot.build_identifier, hence the bot parameter here.
        But then we also need bot for the unpickling so we save it here at module level.
        """
        SlackExtendedBackend.__build_identifier = self.build_identifier
        for cls in (SlackPerson, SlackRoomOccupant, SlackRoom):
            copyreg.pickle(
                cls,
                SlackExtendedBackend._pickle_identifier,
                SlackExtendedBackend._unpickle_identifier,
            )

    def __init__(self, config):
        super().__init__(config)
        identity = config.BOT_IDENTITY
        self.token = identity.get("token", None)
        self.proxies = identity.get("proxies", None)
        if not self.token:
            log.fatal(
                'You need to set your token (found under "Bot Integration" on Slack) in '
                "the BOT_IDENTITY setting in your configuration. Without this token I "
                "cannot connect to Slack."
            )
            sys.exit(1)
        self.sc = None  # Will be initialized in serve_once
        compact = config.COMPACT_OUTPUT if hasattr(config, "COMPACT_OUTPUT") else False
        self.md = slack_markdown_converter(compact)
        self._register_identifiers_pickling()

        # stock events
        self.stock_events = [
            "hello",
            "presence_change",
            "message",
            "member_joined_channel",
            "reaction_added",
            "reaction_removed",
        ]

        # be careful not to overwrite any events that the slack backend handles by default
        # This will cause things to break unexpectedly!
        # These events come straight from the slack api's event types
        self.extra_events = [
            "channel_created",
            "channel_deleted",
            "channel_archive",
            "channel_rename",
            "channel_unarchive",
            "pin_added",
            "pin_removed",
            "star_added",
            "star_removed",
        ]
        # use _make_handler_function to dynamically create a class method for each of our event types
        # This method will call self._dispatch_to_plugins with callback_{event_type} and the message as args.
        # _dispatch_to_plugins takes care of calling all the potential callbacks in all plugins for us and passes
        # along the message as an argument
        for event in self.extra_events:
            # add a handler in for any event type that doesn't have a handler already created
            if getattr(self, f"_{event}_event_handler", None) is None:
                setattr(
                    self,
                    f"_{event}_event_handler",
                    MethodType(_make_handler_function(f"callback_{event}"), self),
                )

    def set_message_size_limit(self, limit=4096, hard_limit=40000):
        """
        Slack supports upto 40000 characters per message, Errbot maintains 4096 by default.
        """
        super().set_message_size_limit(limit, hard_limit)

    def api_call(self, method, data=None, raise_errors=True):
        """
        Make an API call to the Slack API and return response data.

        This is a thin wrapper around `SlackClient.server.api_call`.

        :param method:
            The API method to invoke (see https://api.slack.com/methods/).
        :param raise_errors:
            Whether to raise :class:`~SlackAPIResponseError` if the API
            returns an error
        :param data:
            A dictionary with data to pass along in the API request.
        :returns:
            A dictionary containing the (JSON-decoded) API response
        :raises:
            :class:`~SlackAPIResponseError` if raise_errors is True and the
            API responds with `{"ok": false}`
        """
        if data is None:
            data = {}
        response = self.sc.api_call(method, **data)
        if not isinstance(response, collections.Mapping):
            # Compatibility with SlackClient < 1.0.0
            response = json.loads(response.decode("utf-8"))

        if raise_errors and not response["ok"]:
            raise SlackAPIResponseError(
                f"Slack API call to {method} failed: {response['error']}",
                error=response["error"],
            )
        return response

    def update_alternate_prefixes(self):
        """Converts BOT_ALT_PREFIXES to use the slack ID instead of name

        Slack only acknowledges direct callouts `@username` in chat if referred
        by using the ID of that user.
        """
        # convert BOT_ALT_PREFIXES to a list
        try:
            bot_prefixes = self.bot_config.BOT_ALT_PREFIXES.split(",")
        except AttributeError:
            bot_prefixes = list(self.bot_config.BOT_ALT_PREFIXES)

        converted_prefixes = []
        for prefix in bot_prefixes:
            try:
                converted_prefixes.append(f"<@{self.username_to_userid(prefix)}>")
            except Exception as e:
                log.error(
                    'Failed to look up Slack userid for alternate prefix "%s": %s',
                    prefix,
                    e,
                )

        self.bot_alt_prefixes = tuple(
            x.lower() for x in self.bot_config.BOT_ALT_PREFIXES
        )
        log.debug("Converted bot_alt_prefixes: %s", self.bot_config.BOT_ALT_PREFIXES)

    def serve_once(self):
        self.sc = SlackClient(self.token, proxies=self.proxies)

        log.info("Verifying authentication token")
        self.auth = self.api_call("auth.test", raise_errors=False)
        if not self.auth["ok"]:
            raise SlackAPIResponseError(
                error=f"Couldn't authenticate with Slack. Server said: {self.auth['error']}"
            )
        log.debug("Token accepted")
        self.bot_identifier = SlackPerson(self.sc, self.auth["user_id"])

        log.info("Connecting to Slack real-time-messaging API")
        if self.sc.rtm_connect():
            log.info("Connected")
            # Block on reads instead of using the busy loop suggested in slackclient docs
            # https://github.com/slackapi/python-slackclient/issues/46#issuecomment-165674808
            self.sc.server.websocket.sock.setblocking(True)
            self.reset_reconnection_count()

            # Inject bot identity to alternative prefixes
            self.update_alternate_prefixes()

            try:
                while True:
                    for message in self.sc.rtm_read():
                        self._dispatch_slack_message(message)
            except KeyboardInterrupt:
                log.info("Interrupt received, shutting down..")
                return True
            except Exception:
                log.exception("Error reading from RTM stream:")
            finally:
                log.debug("Triggering disconnect callback")
                self.disconnect_callback()
        else:
            raise Exception("Connection failed, invalid token ?")

    def _dispatch_slack_message(self, message):
        """
        Process an incoming message from slack.

        """
        if "type" not in message:
            log.debug("Ignoring non-event message: %s.", message)
            return

        event_type = message["type"].lower().rstrip()

        event_handler = getattr(self, f"_{event_type}_event_handler", None)

        if event_handler is None:
            log.debug(
                "No event handler available for %s, ignoring this event", event_type
            )
            return
        try:
            log.debug("Processing slack event: %s", message)
            event_handler(message)
        except Exception:
            log.exception(f"{event_type} event handler raised an exception")

    def _dispatch_to_plugins(self, method, *args, **kwargs):
        """
        Dispatch the given method to all active plugins.
        Checks if the plugin supports that method before calling it. This is mainly for our special slack methods
        Will catch and log any exceptions that occur.
        :param method: The name of the function to dispatch.
        :param *args: Passed to the callback function.
        :param **kwargs: Passed to the callback function.
        """
        for plugin in self.plugin_manager.get_all_active_plugins():
            plugin_name = plugin.name
            log.debug("Triggering %s on %s.", method, plugin_name)
            # noinspection PyBroadException
            plugin_method = getattr(plugin, method, None)
            if plugin_method is not None:
                try:
                    plugin_method(*args, **kwargs)
                except Exception:
                    log.exception("%s on %s crashed.", method, plugin_name)
            else:
                log.debug(
                    "Not triggering %s for %s because it does not support slack-extended",
                    method,
                    plugin_name,
                )

    def _hello_event_handler(self, event):
        """Event handler for the 'hello' event"""
        self.connect_callback()
        self.callback_presence(Presence(identifier=self.bot_identifier, status=ONLINE))

    def _presence_change_event_handler(self, event):
        """Event handler for the 'presence_change' event"""

        idd = SlackPerson(self.sc, event["user"])
        presence = event["presence"]
        # According to https://api.slack.com/docs/presence, presence can
        # only be one of 'active' and 'away'
        if presence == "active":
            status = ONLINE
        elif presence == "away":
            status = AWAY
        else:
            log.error(
                f"It appears the Slack API changed, I received an unknown presence type {presence}."
            )
            status = ONLINE
        self.callback_presence(Presence(identifier=idd, status=status))

    def _message_event_handler(self, event):
        """Event handler for the 'message' event"""
        channel = event["channel"]
        if channel[0] not in "CGD":
            log.warning("Unknown message type! Unable to handle %s", channel)
            return

        subtype = event.get("subtype", None)

        if subtype in ("message_deleted", "channel_topic", "message_replied"):
            log.debug("Message of type %s, ignoring this event", subtype)
            return

        if subtype == "message_changed" and "attachments" in event["message"]:
            # If you paste a link into Slack, it does a call-out to grab details
            # from it so it can display this in the chatroom. These show up as
            # message_changed events with an 'attachments' key in the embedded
            # message. We should completely ignore these events otherwise we
            # could end up processing bot commands twice (user issues a command
            # containing a link, it gets processed, then Slack triggers the
            # message_changed event and we end up processing it again as a new
            # message. This is not what we want).
            log.debug(
                "Ignoring message_changed event with attachments, likely caused "
                "by Slack auto-expanding a link"
            )
            return

        if "message" in event:
            text = event["message"].get("text", "")
            user = event["message"].get("user", event.get("bot_id"))
        else:
            text = event.get("text", "")
            user = event.get("user", event.get("bot_id"))

        text, mentioned = self.process_mentions(text)

        text = self.sanitize_uris(text)

        log.debug("Saw an event: %s", pprint.pformat(event))
        log.debug("Escaped IDs event text: %s", text)

        msg = Message(
            text,
            extras={
                "attachments": event.get("attachments"),
                "slack_event": event,
            },
        )

        if channel.startswith("D"):
            if subtype == "bot_message":
                msg.frm = SlackBot(
                    self.sc,
                    bot_id=event.get("bot_id"),
                    bot_username=event.get("username", ""),
                )
            else:
                msg.frm = SlackPerson(self.sc, user, event["channel"])
            msg.to = SlackPerson(
                self.sc,
                self.username_to_userid(self.sc.server.username),
                event["channel"],
            )
            channel_link_name = event["channel"]
        else:
            if subtype == "bot_message":
                msg.frm = SlackRoomBot(
                    self.sc,
                    bot_id=event.get("bot_id"),
                    bot_username=event.get("username", ""),
                    channelid=event["channel"],
                    bot=self,
                )
            else:
                msg.frm = SlackRoomOccupant(self.sc, user, event["channel"], bot=self)
            msg.to = SlackRoom(channelid=event["channel"], bot=self)
            channel_link_name = msg.to.name

        msg.extras["url"] = (
            f"https://{self.sc.server.domain}.slack.com/archives/"
            f'{channel_link_name}/p{self._ts_for_message(msg).replace(".", "")}'
        )

        self.callback_message(msg)

        if mentioned:
            self.callback_mention(msg, mentioned)

    def _member_joined_channel_event_handler(self, event):
        """Event handler for the 'member_joined_channel' event"""
        user = SlackPerson(self.sc, event["user"])
        if user == self.bot_identifier:
            self.callback_room_joined(SlackRoom(channelid=event["channel"], bot=self))
        self._dispatch_to_plugins("callback_member_joined_room", event)

    def _reaction_event_handler(self, event):
        """Event handler for the 'reaction_added'
        and 'reaction_removed' events"""

        user = SlackPerson(self.sc, event["user"])
        item_user = None
        if event["item_user"]:
            item_user = SlackPerson(self.sc, event["item_user"])

        action = REACTION_ADDED
        if event["type"] == "reaction_removed":
            action = REACTION_REMOVED

        reaction = Reaction(
            reactor=user,
            reacted_to_owner=item_user,
            action=action,
            timestamp=event["event_ts"],
            reaction_name=event["reaction"],
            reacted_to=event["item"],
        )

        self.callback_reaction(reaction)

    def userid_to_username(self, id_):
        """Convert a Slack user ID to their user name"""
        user = self.sc.server.users.get(id_)
        if user is None:
            raise UserDoesNotExistError(f"Cannot find user with ID {id_}.")
        return user.name

    def username_to_userid(self, name):
        """Convert a Slack user name to their user ID"""
        name = name.lstrip("@")
        user = self.sc.server.users.find(name)
        if user is None:
            raise UserDoesNotExistError(f"Cannot find user {name}.")
        return user.id

    def channelid_to_channelname(self, id_):
        """Convert a Slack channel ID to its channel name"""
        channel = [channel for channel in self.sc.server.channels if channel.id == id_]
        if not channel:
            raise RoomDoesNotExistError(f"No channel with ID {id_} exists.")
        return channel[0].name

    def channelname_to_channelid(self, name):
        """Convert a Slack channel name to its channel ID"""
        name = name.lstrip("#")
        channel = [
            channel for channel in self.sc.server.channels if channel.name == name
        ]
        if not channel:
            raise RoomDoesNotExistError(f"No channel named {name} exists")
        return channel[0].id

    def channels(self, exclude_archived=True, joined_only=False, types=room_types):
        """
        Get all channels and groups and return information about them.

        :param exclude_archived:
            Exclude archived channels/groups
        :param joined_only:
            Filter out channels the bot hasn't joined
        :returns:
            A list of channel (https://api.slack.com/types/channel)
            and group (https://api.slack.com/types/group) types.

        See also:
          * https://api.slack.com/methods/conversations.list
        """
        response = self.api_call(
            "conversations.list",
            data={"exclude_archived": exclude_archived, "types": types},
        )
        channels = [
            channel
            for channel in response["channels"]
            if channel["is_member"] or not joined_only
        ]

        # There is no need to list groups anymore.  Groups are now identified as 'private_channel'
        # using the conversations.list api method.
        # response = self.api_call('groups.list', data={'exclude_archived': exclude_archived})
        # No need to filter for 'is_member' in this next call (it doesn't
        # (even exist) because leaving a group means you have to get invited
        # back again by somebody else.
        # groups = [group for group in response['groups']]

        return channels

    @lru_cache(1024)
    def get_im_channel(self, id_):
        """Open a direct message channel to a user"""
        try:
            response = self.api_call("conversations.open", data={"users": id_})
            return response["channel"]["id"]
        except SlackAPIResponseError as e:
            if e.error == "cannot_dm_bot":
                log.info("Tried to DM a bot.")
                return None
            else:
                raise e

    def _prepare_message(self, msg):  # or card
        """
        Translates the common part of messaging for Slack.
        :param msg: the message you want to extract the Slack concept from.
        :return: a tuple to user human readable, the channel id
        """
        if msg.is_group:
            to_channel_id = msg.to.id
            to_humanreadable = (
                msg.to.name
                if msg.to.name
                else self.channelid_to_channelname(to_channel_id)
            )
        else:
            to_humanreadable = msg.to.username
            to_channel_id = msg.to.channelid
            if to_channel_id.startswith("C"):
                log.debug(
                    "This is a divert to private message, sending it directly to the user."
                )
                to_channel_id = self.get_im_channel(
                    self.username_to_userid(msg.to.username)
                )
        return to_humanreadable, to_channel_id

    def send_message(self, msg):
        super().send_message(msg)

        if msg.parent is not None:
            # we are asked to reply to a specify thread.
            try:
                msg.extras["thread_ts"] = self._ts_for_message(msg.parent)
            except KeyError:
                # Gives to the user a more interesting explanation if we cannot find a ts from the parent.
                log.exception(
                    "The provided parent message is not a Slack message "
                    "or does not contain a Slack timestamp."
                )

        to_humanreadable = "<unknown>"
        try:
            if msg.is_group:
                to_channel_id = msg.to.id
                to_humanreadable = (
                    msg.to.name
                    if msg.to.name
                    else self.channelid_to_channelname(to_channel_id)
                )
            else:
                to_humanreadable = msg.to.username
                if isinstance(
                    msg.to, RoomOccupant
                ):  # private to a room occupant -> this is a divert to private !
                    log.debug(
                        "This is a divert to private message, sending it directly to the user."
                    )
                    to_channel_id = self.get_im_channel(
                        self.username_to_userid(msg.to.username)
                    )
                else:
                    to_channel_id = msg.to.channelid

            msgtype = "direct" if msg.is_direct else "channel"
            log.debug(
                "Sending %s message to %s (%s).",
                msgtype,
                to_humanreadable,
                to_channel_id,
            )
            body = self.md.convert(msg.body)
            log.debug("Message size: %d.", len(body))

            parts = self.prepare_message_body(body, self.message_size_limit)

            timestamps = []
            for part in parts:
                data = {
                    "channel": to_channel_id,
                    "text": part,
                    "unfurl_media": "true",
                    "link_names": "1",
                    "as_user": "true",
                }

                # Keep the thread_ts to answer to the same thread.
                if "thread_ts" in msg.extras:
                    data["thread_ts"] = msg.extras["thread_ts"]

                result = self.api_call("chat.postMessage", data=data)
                timestamps.append(result["ts"])

            msg.extras["ts"] = timestamps
        except Exception:
            log.exception(
                f"An exception occurred while trying to send the following message "
                f"to {to_humanreadable}: {msg.body}."
            )

    def _slack_upload(self, stream: Stream) -> None:
        """
        Performs an upload defined in a stream
        :param stream: Stream object
        :return: None
        """
        try:
            stream.accept()
            resp = self.api_call(
                "files.upload",
                data={
                    "channels": stream.identifier.channelid,
                    "filename": stream.name,
                    "file": stream,
                },
            )
            if "ok" in resp and resp["ok"]:
                stream.success()
            else:
                stream.error()
        except Exception:
            log.exception(
                f"Upload of {stream.name} to {stream.identifier.channelname} failed."
            )

    def send_stream_request(
        self,
        user: Identifier,
        fsource: BinaryIO,
        name: str = None,
        size: int = None,
        stream_type: str = None,
    ) -> Stream:
        """
        Starts a file transfer. For Slack, the size and stream_type are unsupported

        :param user: is the identifier of the person you want to send it to.
        :param fsource: is a file object you want to send.
        :param name: is an optional filename for it.
        :param size: not supported in Slack backend
        :param stream_type: not supported in Slack backend

        :return Stream: object on which you can monitor the progress of it.
        """
        stream = Stream(user, fsource, name, size, stream_type)
        log.debug(
            "Requesting upload of %s to %s (size hint: %d, stream type: %s).",
            name,
            user.channelname,
            size,
            stream_type,
        )
        self.thread_pool.apply_async(self._slack_upload, (stream,))
        return stream

    def send_card(self, card: Card):
        if isinstance(card.to, RoomOccupant):
            card.to = card.to.room
        to_humanreadable, to_channel_id = self._prepare_message(card)
        attachment = {}
        if card.summary:
            attachment["pretext"] = card.summary
        if card.title:
            attachment["title"] = card.title
        if card.link:
            attachment["title_link"] = card.link
        if card.image:
            attachment["image_url"] = card.image
        if card.thumbnail:
            attachment["thumb_url"] = card.thumbnail

        if card.color:
            attachment["color"] = (
                COLORS[card.color] if card.color in COLORS else card.color
            )

        if card.fields:
            attachment["fields"] = [
                {"title": key, "value": value, "short": True}
                for key, value in card.fields
            ]

        parts = self.prepare_message_body(card.body, self.message_size_limit)
        part_count = len(parts)
        footer = attachment.get("footer", "")
        for i in range(part_count):
            if part_count > 1:
                attachment["footer"] = f"{footer} [{i + 1}/{part_count}]"
            attachment["text"] = parts[i]
            data = {
                "channel": to_channel_id,
                "attachments": json.dumps([attachment]),
                "link_names": "1",
                "as_user": "true",
            }
            try:
                log.debug("Sending data:\n%s", data)
                self.api_call("chat.postMessage", data=data)
            except Exception:
                log.exception(
                    f"An exception occurred while trying to send a card to {to_humanreadable}.[{card}]"
                )

    def __hash__(self):
        return 0  # this is a singleton anyway

    def change_presence(self, status: str = ONLINE, message: str = "") -> None:
        self.api_call(
            "users.setPresence",
            data={"presence": "auto" if status == ONLINE else "away"},
        )

    @staticmethod
    def prepare_message_body(body, size_limit):
        """
        Returns the parts of a message chunked and ready for sending.

        This is a staticmethod for easier testing.

        Args:
            body (str)
            size_limit (int): chunk the body into sizes capped at this maximum

        Returns:
            [str]

        """
        fixed_format = body.startswith("```")  # hack to fix the formatting
        parts = list(split_string_after(body, size_limit))

        if len(parts) == 1:
            # If we've got an open fixed block, close it out
            if parts[0].count("```") % 2 != 0:
                parts[0] += "\n```\n"
        else:
            for i, part in enumerate(parts):
                starts_with_code = part.startswith("```")

                # If we're continuing a fixed block from the last part
                if fixed_format and not starts_with_code:
                    parts[i] = "```\n" + part

                # If we've got an open fixed block, close it out
                if part.count("```") % 2 != 0:
                    parts[i] += "\n```\n"

        return parts

    @staticmethod
    def extract_identifiers_from_string(text):
        """
        Parse a string for Slack user/channel IDs.

        Supports strings with the following formats::

            <#C12345>
            <@U12345>
            <@U12345|user>
            @user
            #channel/user
            #channel

        Returns the tuple (username, userid, channelname, channelid).
        Some elements may come back as None.
        """
        exception_message = (
            "Unparseable slack identifier, should be of the format `<#C12345>`, `<@U12345>`, "
            "`<@U12345|user>`, `@user`, `#channel/user` or `#channel`. (Got `%s`)"
        )
        text = text.strip()

        if text == "":
            raise ValueError(exception_message % "")

        channelname = None
        username = None
        channelid = None
        userid = None

        if text[0] == "<" and text[-1] == ">":
            exception_message = (
                "Unparseable slack ID, should start with U, B, C, G, D or W (got `%s`)"
            )
            text = text[2:-1]
            if text == "":
                raise ValueError(exception_message % "")
            if text[0] in ("U", "B", "W"):
                if "|" in text:
                    userid, username = text.split("|")
                else:
                    userid = text
            elif text[0] in ("C", "G", "D"):
                channelid = text
            else:
                raise ValueError(exception_message % text)
        elif text[0] == "@":
            username = text[1:]
        elif text[0] == "#":
            plainrep = text[1:]
            if "/" in text:
                channelname, username = plainrep.split("/", 1)
            else:
                channelname = plainrep
        else:
            raise ValueError(exception_message % text)

        return username, userid, channelname, channelid

    def build_identifier(self, txtrep):
        """
        Build a :class:`SlackIdentifier` from the given string txtrep.

        Supports strings with the formats accepted by
        :func:`~extract_identifiers_from_string`.
        """
        log.debug("building an identifier from %s.", txtrep)
        username, userid, channelname, channelid = self.extract_identifiers_from_string(
            txtrep
        )

        if userid is None and username is not None:
            userid = self.username_to_userid(username)
        if channelid is None and channelname is not None:
            channelid = self.channelname_to_channelid(channelname)
        if userid is not None and channelid is not None:
            return SlackRoomOccupant(self.sc, userid, channelid, bot=self)
        if userid is not None:
            return SlackPerson(self.sc, userid, self.get_im_channel(userid))
        if channelid is not None:
            return SlackRoom(channelid=channelid, bot=self)

        raise Exception(
            "You found a bug. I expected at least one of userid, channelid, username or channelname "
            "to be resolved but none of them were. This shouldn't happen so, please file a bug."
        )

    def is_from_self(self, msg: Message) -> bool:
        return self.bot_identifier.userid == msg.frm.userid

    def build_reply(self, msg, text=None, private=False, threaded=False):
        response = self.build_message(text)

        if "thread_ts" in msg.extras["slack_event"]:
            # If we reply to a threaded message, keep it in the thread.
            response.extras["thread_ts"] = msg.extras["slack_event"]["thread_ts"]
        elif threaded:
            # otherwise check if we should start a new thread
            response.parent = msg

        response.frm = self.bot_identifier
        if private:
            response.to = msg.frm
        else:
            response.to = msg.frm.room if isinstance(msg.frm, RoomOccupant) else msg.frm
        return response

    def add_reaction(self, msg: Message, reaction: str) -> None:
        """
        Add the specified reaction to the Message if you haven't already.
        :param msg: A Message.
        :param reaction: A str giving an emoji, without colons before and after.
        :raises: ValueError if the emoji doesn't exist.
        """
        return self._react("reactions.add", msg, reaction)

    def remove_reaction(self, msg: Message, reaction: str) -> None:
        """
        Remove the specified reaction from the Message if it is currently there.
        :param msg: A Message.
        :param reaction: A str giving an emoji, without colons before and after.
        :raises: ValueError if the emoji doesn't exist.
        """
        return self._react("reactions.remove", msg, reaction)

    def _react(self, method: str, msg: Message, reaction: str) -> None:
        try:
            # this logic is from send_message
            if msg.is_group:
                to_channel_id = msg.to.id
            else:
                to_channel_id = msg.to.channelid

            ts = self._ts_for_message(msg)

            self.api_call(
                method,
                data={"channel": to_channel_id, "timestamp": ts, "name": reaction},
            )
        except SlackAPIResponseError as e:
            if e.error == "invalid_name":
                raise ValueError(e.error, "No such emoji", reaction)
            elif e.error in ("no_reaction", "already_reacted"):
                # This is common if a message was edited after you reacted to it, and you reacted to it again.
                # Chances are you don't care about this. If you do, call api_call() directly.
                pass
            else:
                raise SlackAPIResponseError(error=e.error)

    def _ts_for_message(self, msg):
        try:
            return msg.extras["slack_event"]["message"]["ts"]
        except KeyError:
            return msg.extras["slack_event"]["ts"]

    def shutdown(self):
        super().shutdown()

    @property
    def mode(self):
        return "slack"

    def query_room(self, room):
        """ Room can either be a name or a channelid """
        if room.startswith("C") or room.startswith("G"):
            return SlackRoom(channelid=room, bot=self)

        m = SLACK_CLIENT_CHANNEL_HYPERLINK.match(room)
        if m is not None:
            return SlackRoom(channelid=m.groupdict()["id"], bot=self)

        return SlackRoom(name=room, bot=self)

    def rooms(self):
        """
        Return a list of rooms the bot is currently in.

        :returns:
            A list of :class:`~SlackRoom` instances.
        """
        channels = self.channels(
            joined_only=True,
            exclude_archived=True,
        )
        return [SlackRoom(channelid=channel["id"], bot=self) for channel in channels]

    def prefix_groupchat_reply(self, message, identifier):
        super().prefix_groupchat_reply(message, identifier)
        message.body = f"@{identifier.nick}: {message.body}"

    @staticmethod
    def sanitize_uris(text):
        """
        Sanitizes URI's present within a slack message. e.g.
        <mailto:example@example.org|example@example.org>,
        <http://example.org|example.org>
        <http://example.org>

        :returns:
            string
        """
        text = re.sub(r"<([^|>]+)\|([^|>]+)>", r"\2", text)
        text = re.sub(r"<(http([^>]+))>", r"\1", text)

        return text

    def process_mentions(self, text):
        """
        Process mentions in a given string
        :returns:
            A formatted string of the original message
            and a list of :class:`~SlackPerson` instances.
        """
        mentioned = []

        m = re.findall("<@[^>]*>*", text)

        for word in m:
            try:
                identifier = self.build_identifier(word)
            except Exception as e:
                log.debug(
                    "Tried to build an identifier from '%s' but got exception: %s",
                    word,
                    e,
                )
                continue

            # We only track mentions of persons.
            if isinstance(identifier, SlackPerson):
                log.debug("Someone mentioned")
                mentioned.append(identifier)
                text = text.replace(word, str(identifier))

        return text, mentioned
