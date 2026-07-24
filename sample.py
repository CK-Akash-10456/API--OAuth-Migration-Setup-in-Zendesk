from OAuth_Generate import ZendeskOAuth
import requests

auth = ZendeskOAuth()
resp = requests.get(
    f"https://{auth.subdomain}.zendesk.com/api/v2/users/me.json",
    auth=auth,
)
data = resp.json()["user"]
print(f"OK — connected as {data['name']} ({data['role']}), "
      f"token TTL: {auth.ttl}s, age: {auth.age:.0f}s")
