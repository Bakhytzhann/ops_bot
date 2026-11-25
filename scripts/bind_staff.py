import argparse

from services.station_notes import ROLES, notes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("telegram_id", type=int)
    parser.add_argument("employee_id")
    parser.add_argument("role", choices=sorted(ROLES))
    parser.add_argument(
        "--verified",
        action="store_true",
        required=True,
        help="Confirm identity was checked outside Telegram",
    )
    args = parser.parse_args()
    notes.bind(args.telegram_id, args.employee_id, args.role)
    print("Verified staff binding saved.")


if __name__ == "__main__":
    main()
