import json
import pandas as pd

def query_issues(issues: list) -> pd.DataFrame:
    titles = []
    bodys = []
    numbers = []
    for issue in issues:
        number = issue.get("number", 0)
        title = issue.get("title", "")
        body = issue.get("body", "")
        titles.append(title)
        bodys.append(body)
        numbers.append(number)
    table = pd.DataFrame(
        {
            "title": titles,
            "body": bodys,
            "number": numbers,
        }
    ).reset_index(drop=True)
    return table


def main():
    with open('src/scripts/issues.json', 'r') as f:
        issues = json.load(f)
    
    queried_issues = query_issues(issues)
    queried_issues.to_csv('src/scripts/queried_issues.csv', index=False)

if __name__ == '__main__':
    main()
