PowerSpyCli
===========

PowerSpyCli is a tool to connect and collect power metrics from a [PowerSpy 2 powermeter](https://www.alciom.com/nos-metiers/produits/powerspy2/).

PowerSpyCli is tested on GNU/Linux and works with Python 3.

## Installation

To run PowerSpyCli, you need to install ```bluez``` and ```python-bluez```.

On Debian/Ubuntu: ```apt install bluez python-bluez```. On Arch-based distros: install ```bluez``` and ```python-pybluez```.

Then clone the repo (or just download the ```powerspycli.py``` file), and run it.

### Installing on SailfishOS

On the Linux-based mobile OS, SailfishOS, you can run PowerSpyCli by installing the following packages:
```
devel-su zypper install bluez5-libs-devel make gcc glib2-devel
python3 -m venv venv
source venv/bin/activate
pip install wheel
pip install pybluez
```

## Running PowerSpyCli

To run PowerSpyCli and start collecting power metrics, just run the python file: ```python powerspycli.py``` or directly ```./powerspycli.py```.

The tool requires the bluetooth device address of the PowerSpy in order to run: ```./powerspycli.py 00:11:22:33:44:55```.

### Arguments

PowerSpyCli, by default, will display the power consumption of the PowerSpy every second.
To show all the collected metrics (i.e., voltage, ampere, etc.), run it with the ```-a``` argument.

The ```-v``` argument will display all the logs and connection info (verbose mode).

To save the power data along with the timestamp to a CSV file, use the ```-f``` argument:
```./powerspycli.py 00:11:22:33:44:55 -f file.csv```.

## License

PowerSpyCli is forked from: https://github.com/patrickmarlier/powerspy.py/ with support for Python 3 and additional updates.

It is licensed under the GNU Lesser General Public License v3.0 or later (LGPL-3.0-or-later).

Copyright (c) 2021, Université de Pau et des Pays de l'Adour.
All rights reserved. This program and the accompanying materials are made available under the terms of the  GNU Lesser General Public License v3.0 or later (LGPL-3.0-or-later) which accompanies this distribution, and is available at: https://www.gnu.org/licenses/lgpl-3.0.en.html
