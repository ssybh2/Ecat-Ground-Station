# Third-party interfaces

`ecat_groundstation/message_fields.json` records the ROS command field names and
types from the `custom_msgs/msg/Write*.msg` interfaces in
https://github.com/AIMEtherCAT/EcatV2_Master at commit
`7c1bd8a3c7f47cc8505470640f8dd7baf5698d76`.

The original project is licensed under GNU LGPL version 2.1. Its license text is
included as `LICENSE`. Original authors retain their rights in those interfaces.

Wire tags, sizes and field names were checked against that project and its
official TaskEditor. No upstream backend implementation is bundled or modified.

## Optional H750 GNU C++ runtime

The lightweight H750 builder downloads selected, unmodified GNU libstdc++ and
libsupc++ source files from the GCC 10.3.0 release, commit
`f00b5710a30f22efc3171c393e56aeb335c3cd39`, and builds them locally for Cortex-M7.
The source catalog contains the original paths and SHA256 checksums. GNU sources,
their copyright notices, `COPYING3`, and `COPYING.RUNTIME` are retained in the
user's build cache. Those GNU components use GPLv3 (or later) with the GCC Runtime
Library Exception 3.1. No GNU source or compiled runtime archive is included in
the GroundStation wheel.
