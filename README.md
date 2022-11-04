# OpenParliamentTV-Conductor

General conductor script for OpenParliamentTV

This script is used to setup a new configuration for the
OpenParliamentTV data backend.

## Setup of a new backend server

- Clone the OpenParliamentTV-Conductor repository

- Run `optv init` to initialize the repository structure. It will clone
  the -Tools repository for code and the -Data-DE repository for data
  files.

- Run `optv check` to  make sure all dependencies are present (repositories, libs...)

- Run `optv update` to do a basic update of the data in the data
  repository. The period is specified at the beginning of the `optv`
  script.

- Run `optv fullupdate` to do an update of the data and also run the
  time-alignment and NER extraction components.

