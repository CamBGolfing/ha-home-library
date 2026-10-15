# Changelog

## 1.3.0

- Name your library and give it your own logo. The owner sets both under Account, with a live preview of the home-screen icon.
- The name and logo show in the browser tab, the header, the sign-in and invite pages, and when someone adds the library to their phone's home screen.
- Pictures are cropped or fitted to a square in the browser before upload, so any PNG, JPG or SVG works and the server only ever stores PNG files.

## 1.2.0

- Much more reliable scanning on iPhone. Safari has no built-in barcode reader, so the app now uses ZXing (C++, compiled to WebAssembly) instead of the old JavaScript decoder. In testing on the same set of photos it read more than three times as many barcodes. Chrome on Android keeps using its built-in reader.
- Reads the area inside the on-screen box at the camera's full resolution, and asks the camera for 1080p.
- On iPhones with several back lenses, prefers Apple's multi-lens camera, which can switch to the close-focus lens. A camera picker appears when there's more than one back camera, and your choice is remembered.
- Zoom and flashlight buttons when the camera supports them.
- New "Take a photo" option: snap the barcode with the phone's own camera app (which always focuses up close) and the app reads the picture.

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
