# Retired VKMS experiment

The old VKMS experiment has been retired. Super Screen uses owned Mutter
`RecordVirtual` sessions and does not install a display driver, load a kernel
module, or require a VKMS output.

Existing physical or external virtual outputs can still be listed and toggled
in the console. These controls do not create or revive VKMS devices.

Local retirement logs and display-layout backups belong in the user's private
state directory. They are not runtime inputs and are not shipped in releases.
For the current implementation, see [Super Screen lifecycle](../super-screen-lifecycle.md).
