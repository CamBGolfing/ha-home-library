# Changelog

## 1.1.0

- Accounts and sign-in. Three levels of access: Owner, Family (add, edit, lend) and Friend (browse and ask to borrow).
- Home Assistant users are signed in automatically from the sidebar. The first person to open the library becomes its owner.
- The Library sidebar panel is now visible to non-admin Home Assistant users.
- Invite links for friends who don't have a Home Assistant login. They sign in on the library's own address and never see Home Assistant.
- Friends can ask to borrow a book; requests show up on the Loans page to approve or decline.
- Friends see whether a book is checked out, but not who has it, and don't see private notes.
- Password reset links, turning access off, and a record of who added each book.
- The direct port (8099) now requires a library sign-in.

## 1.0.0

- First release: barcode scanning, Open Library and Google Books lookup with local covers, shelves, tags, wishlist, loans, CSV export.
