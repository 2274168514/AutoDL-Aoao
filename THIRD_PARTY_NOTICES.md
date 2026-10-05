# Third-party components

The command-line monitor uses the Python standard library. The desktop Chrome
connection additionally uses **websocket-client 1.9.2**, distributed under the
Apache License 2.0: https://github.com/websocket-client/websocket-client

The bundled desktop application includes this package's distribution metadata
and original license under `_internal/websocket_client-1.9.2.dist-info/`.

The desktop bundle also includes Python, Tcl/Tk and their runtime components.
The Python installation's original license is copied without modification to
`licenses/PYTHON-LICENSE.txt` beside the application. The Windows Python
distribution's file also contains its bundled-library notices. Additional
runtime license files, such as `_internal/_tk_data/license.terms`, remain in
their original packaged locations. PyInstaller is used to build the executable;
its bootloader exception permits distribution of the resulting application
under this project's MIT license.

Google Chrome is installed and maintained separately by the user. It is not
included in the desktop bundle. AutoDL and Google do not sponsor this project.

The application icon is derived from the user-provided `assets/labubu.jpg`.
The project's MIT license covers its code; it does not grant rights to
third-party characters, artwork or trademarks depicted in the icon.
