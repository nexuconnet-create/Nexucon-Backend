import os
from xhtml2pdf import pisa

def test_pdf():
    html = """
    <html>
    <head>
    <style>
    @page {
        size: a4 portrait;
        margin: 2cm;
        @frame footer {
            -pdf-frame-content: footer_content;
            bottom: 1cm;
            margin-left: 2cm;
            margin-right: 2cm;
            height: 1cm;
        }
    }
    body { font-family: Helvetica; color: #334155; }
    h1 { color: #1E293B; }
    </style>
    </head>
    <body>
    <div id="footer_content">Footer text <pdf:pagenumber/></div>
    <h1>Hello World</h1>
    <p>This is a test.</p>
    </body>
    </html>
    """
    with open("test.pdf", "wb") as f:
        pisa.CreatePDF(html, dest=f)
    print("Success!")

if __name__ == "__main__":
    test_pdf()
