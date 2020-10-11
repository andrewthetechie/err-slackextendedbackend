# SlackExtendedBackend

The great experiment!
 
![the great experiment](https://vignette.wikia.nocookie.net/memoryalpha/images/9/9b/USS_Excelsior_stalls_outside_Spacedock.jpg/revision/latest?cb=20090606201617&path-prefix=en)

# What is this?
This is an extended slack backend to give sadevbot some extra special slack powers. 

It extends the stock errbot slack backend to fire more events as callbacks to plugins. 

# Why?
Because of errbot's pluggable nature, it by default doesn't support a lot of slack native features. Because Errbot is
built to support multiple chat platforms, its backends lack the ability to forward random api events from Slack on to 
plugins. After a few hacky attempts to add in events to the new SlackRTM backend (and finding that backend is still in 
beta), the ExtendedBackend was born

# How?
Add event types from the Slack RTM API to the extra_events list in __init__. 

If you are ok with the default behavior (calling _dispatch_plugin("callback_{event_name}, message)) then you're done. 

If not, you can add a new function to the class named {event_type}_hander that takes as an argument a message object 
(the slack event object). Inside this function, you can do whatever extra processing you need then all 
self._dispatch_to_plugins for your callback. Just be sure to update the documentation if your callback has a custom 
signature

## Things that might cause issues
The more events we call, the more potential callbacks we are calling. Errbot is not exactly crazy performant. Its a bit 
unknown how far out we can scale it with more threads before things get really hairy. Adding event handlers for things 
like user_typing could be a problem if they're getting fired a lot.

# Extra Events

* member_joined_channel: callback_member_joined_room(self, message: Dict[str])
* channel_created: callback_channel_created(self, message: Dict[str])
* channel_deleted: callback_channel_deleted(self, message: Dict[str])
* channel_archive: callback_channel_archive(self, message: Dict[str])
* channel_rename: callback_channel_rename(self, message: Dict[str])
* channel_unarchive: callback_channel_unarchive(self, message: Dict[str])
* pin_added: callback_pin_added(self, message: Dict[str])
* pin_removed: callback_pin_removed(self, message: Dict[str])
* star_added: callback_star_added(self, message: Dict[str])
* star_removed' callback_star_removed(self, message: Dict[str])

Events who's signature indicates "message" as an argument recieve the event dict the slack api sent for the event.

## Using these evvents in a plugin
To use these events in a plugin, just implement a method with the same name and signature as the callback listed in the 
list above, then do whatever you need to do!
