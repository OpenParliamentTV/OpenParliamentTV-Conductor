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

- Run `optv update` to do the base update of the data in the data
  repository + time alignment. The period is configured at the
  beginning of the `optv` script.

- Run `optv publish` to commit the changed files in git and push to
  the source repository.

- Run `optv fullupdate` to do an update of the data and also run the
  time-alignment and NER extraction components.

`update` and `fullupdate` store a log of the actions in
`optv-update.log`. It can be used to monitor progress (with `tail -f`
for instance) and identify actions that have been run afterwards.

## Day-to-day operation

`optv` can accept multiple commands. For a day-to-day operation, we
want to update info from the repository (in case another source
updated the data), execute an update, then publish the data. We obtain
this by running:

`optv pull update publish`

This command can be put in a cron job to be run regularly.
