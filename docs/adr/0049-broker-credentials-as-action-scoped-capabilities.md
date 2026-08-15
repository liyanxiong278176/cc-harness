# Broker credentials as action-scoped capabilities

Background workers and child runs will not inherit long-lived external credentials from the supervisor environment. Model access will preferably be proxied by the supervisor, while other sensitive integrations receive temporary action-scoped capabilities or are executed by a controlled supervisor adapter after approval; this increases broker and adapter complexity but limits credential exposure throughout long-running and potentially adversarial tool execution.
