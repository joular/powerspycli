# <a href="https://www.noureddine.org/research/joular/"><img src="https://raw.githubusercontent.com/joular/.github/main/profile/joular.png" alt="Joular Project" width="64" /></a> PowerSpyCli

PowerSpyCli is a multi-platform tool to connect and collect power metrics from a [PowerSpy 2 powermeter](https://www.alciom.com/nos-metiers/produits/powerspy2/).

PowerSpyCli works in Windows, Linux and macOS with Python 3.

## Installation

Just download the ```powerspycli.py``` file and run it.

## Running PowerSpyCli

To run PowerSpyCli and start collecting power metrics, just run the python file: ```python powerspycli.py``` or directly ```./powerspycli.py```.

The tool requires the PowerSpy2 MAC address in order to run: ```./powerspycli.py 00:11:22:33:44:55```.

### Arguments

PowerSpyCli, by default, will display the power consumption of the PowerSpy every second.
To show all the collected metrics (i.e., voltage, ampere, etc.), run it with the ```-a``` argument.

The ```-v``` argument will display all the logs and connection info (verbose mode).

To save the power data along with the timestamp to a CSV file, use the ```-f``` argument:
```./powerspycli.py 00:11:22:33:44:55 -f file.csv```.

## License

PowerSpyCli is forked from: https://github.com/patrickmarlier/powerspy.py/ with support for Python 3, replacing pyBluez with Python sockets, and many additional new features and updates.

It is licensed under the GNU Lesser General Public License v3.0 or later (LGPL-3.0-or-later).

Copyright (c) 2021-2025, Adel Noureddine, Université de Pau et des Pays de l'Adour.
All rights reserved. This program and the accompanying materials are made available under the terms of the  GNU Lesser General Public License v3.0 or later (LGPL-3.0-or-later) which accompanies this distribution, and is available at: https://www.gnu.org/licenses/lgpl-3.0.en.html
