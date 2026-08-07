import agno.models.openai.chat as m

src = open(m.__file__).read().splitlines()
# print the region around the api_key check (lines ~90-135) plus __post_init__ search
for i in range(85, 140):
    print(f"{i+1}: {src[i]}")
