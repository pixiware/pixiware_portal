"""Blog content registry for the Pixiware marketing blog.

Each entry holds the SEO metadata and FAQ data for one article. The prose body
of each article lives in its own template (templates/blog/<slug>.html); the data
here is what powers the blog index, the XML sitemap and the JSON-LD structured
data (BlogPosting / BreadcrumbList / FAQPage) rendered into every article.

Keeping this in one place means the keyword, title tag, meta description and FAQ
schema for a page can never drift out of sync between the listing and the page
itself.
"""

# The public author / brand shown on every post.
BLOG_AUTHOR = 'Pixiware'
BLOG_NAME = 'Pixiware Blog'
BLOG_DESCRIPTION = (
    'Practical, no-nonsense guides on web design in Bristol — what it '
    'costs, how to choose a designer and how to get a site that actually wins '
    'you customers.'
)

ARTICLES = [
    {
        'slug': 'web-design-cost-bristol',
        'keyword': 'web design cost Bristol',
        'title': 'How Much Does a Website Cost in Bristol? (2026 Guide)',
        'meta_description': (
            'What does web design really cost in Bristol in 2026? Honest price '
            'ranges for brochure sites, small-business websites and online '
            'shops — plus the six things that move the number.'
        ),
        'h1': 'How Much Does a Website Cost in Bristol?',
        'category': 'Pricing',
        'published': '2026-06-16',
        'updated': '2026-07-08',
        'read_time': '9 min read',
        'excerpt': (
            'Real 2026 price ranges for a website in Bristol — from a '
            'simple brochure site to a full online shop — and exactly '
            'what makes the quote go up or down.'
        ),
        'faqs': [
            {
                'q': 'How much does a website cost in Bristol in 2026?',
                'a': (
                    'For a small business in Bristol, expect roughly '
                    '£750–£2,500 for a professional brochure '
                    'site, £2,500–£6,000 for a larger '
                    'custom-designed site, and £5,000–£15,000+ '
                    'for ecommerce. Most local sole traders and small firms '
                    'land in the £1,200–£3,000 range.'
                ),
            },
            {
                'q': 'Is it cheaper to build the website myself?',
                'a': (
                    'A DIY Wix or Squarespace site can cost under £200 a '
                    'year, but you pay in time and lost enquiries. Most '
                    'business owners who start DIY end up hiring a designer '
                    'within 18 months to fix speed, SEO and conversion '
                    'problems, so it is often cheaper to do it properly once.'
                ),
            },
            {
                'q': 'Do web designers in Bristol charge ongoing fees?',
                'a': (
                    'Usually yes — hosting, domain, security updates and '
                    'support are commonly £10–£50 a month, and '
                    'many designers offer optional care plans. Always ask '
                    'what is included and whether you own the site if you '
                    'leave.'
                ),
            },
        ],
    },
    {
        'slug': 'small-business-web-design-bristol',
        'keyword': 'small business web design Bristol',
        'title': 'Small Business Web Design in Bristol: A Practical Guide',
        'meta_description': (
            'A plain-English guide to small business web design in Bristol: '
            'what your site actually needs, what to skip, timelines and how '
            'to brief a designer so you get more local customers.'
        ),
        'h1': 'Small Business Web Design in Bristol',
        'category': 'Small Business',
        'published': '2026-06-23',
        'updated': '2026-07-08',
        'read_time': '8 min read',
        'excerpt': (
            'What a small business website in Bristol genuinely needs to win '
            'local customers — the pages that matter, the ones you can '
            'skip, and how to brief a designer.'
        ),
        'faqs': [
            {
                'q': 'What pages does a small business website need?',
                'a': (
                    'At a minimum: a clear home page, a services or products '
                    'page, an about page that builds trust, contact details '
                    'with a form and map, and customer reviews. For local '
                    'Bristol search, a location-focused page and a Google '
                    'Business Profile are the two highest-impact additions.'
                ),
            },
            {
                'q': 'How long does a small business website take to build?',
                'a': (
                    'A focused small business site in Bristol typically takes '
                    'two to five weeks from kick-off to launch, assuming you '
                    'provide content and images promptly. The single biggest '
                    'cause of delay is waiting on copy and photos, so prepare '
                    'those early.'
                ),
            },
            {
                'q': 'Will a new website help me rank on Google in Bristol?',
                'a': (
                    'A well-built site is the foundation, but ranking locally '
                    'also depends on your Google Business Profile, local '
                    'citations, reviews and content that targets Bristol '
                    'search terms. Good web design makes all of those easier '
                    'and faster to improve.'
                ),
            },
        ],
    },
    {
        'slug': 'ecommerce-web-design-bristol',
        'keyword': 'ecommerce web design Bristol',
        'title': 'Ecommerce Web Design in Bristol: Build a Shop That Sells',
        'meta_description': (
            'How to plan ecommerce web design in Bristol that actually '
            'converts: platform choice, product pages, checkout, delivery and '
            'the local trust signals that turn browsers into buyers.'
        ),
        'h1': 'Ecommerce Web Design in Bristol',
        'category': 'Ecommerce',
        'published': '2026-06-30',
        'updated': '2026-07-08',
        'read_time': '10 min read',
        'excerpt': (
            'From platform choice to checkout, here is how to plan an online '
            'shop in Bristol that turns browsers into buyers — without '
            'overspending on features you do not need.'
        ),
        'faqs': [
            {
                'q': 'Which platform is best for an online shop in Bristol?',
                'a': (
                    'For most Bristol small businesses, Shopify is the '
                    'fastest route to a reliable shop, while WooCommerce '
                    '(WordPress) suits those wanting full control and lower '
                    'ongoing fees. The right choice depends on product count, '
                    'budget and whether you need custom features.'
                ),
            },
            {
                'q': 'How much does an ecommerce website cost in Bristol?',
                'a': (
                    'A small Shopify or WooCommerce shop in Bristol usually '
                    'costs £3,000–£8,000 to design and build, '
                    'plus platform and payment fees. Larger catalogues, '
                    'custom features and integrations push this higher.'
                ),
            },
            {
                'q': 'How do I get my online shop to show up on Google?',
                'a': (
                    'Fast-loading, mobile-friendly product pages with unique '
                    'descriptions, proper product schema, clear categories '
                    'and genuine reviews all help. Pairing this with local '
                    'Bristol content and a Google Business Profile improves '
                    'both local and product visibility.'
                ),
            },
        ],
    },
    {
        'slug': 'freelance-web-designer-bristol',
        'keyword': 'freelance web designer Bristol',
        'title': 'Hiring a Freelance Web Designer in Bristol: What to Look For',
        'meta_description': (
            'Thinking of hiring a freelance web designer in Bristol? The '
            'questions to ask, red flags to avoid, what a fair quote looks '
            'like and how to protect yourself before you pay a deposit.'
        ),
        'h1': 'Hiring a Freelance Web Designer in Bristol',
        'category': 'Hiring',
        'published': '2026-07-03',
        'updated': '2026-07-08',
        'read_time': '8 min read',
        'excerpt': (
            'The questions to ask, the red flags to avoid and what a fair '
            'quote looks like when you hire a freelance web designer in '
            'Bristol.'
        ),
        'faqs': [
            {
                'q': 'How much does a freelance web designer in Bristol charge?',
                'a': (
                    'Freelance web designers in Bristol typically charge '
                    '£25–£75 an hour, or a fixed project price '
                    'of £800–£5,000 depending on scope. Fixed '
                    'quotes are usually safer for a defined project so you '
                    'know the total up front.'
                ),
            },
            {
                'q': 'Freelancer or agency — which is better?',
                'a': (
                    'A freelancer is often cheaper, more personal and ideal '
                    'for small projects; an agency offers more capacity and '
                    'cover for larger or ongoing work. Many Bristol freelancers '
                    'deliver agency-quality results for a fraction of the cost '
                    'on the right project.'
                ),
            },
            {
                'q': 'How do I make sure I own my website?',
                'a': (
                    'Agree in writing that you own the domain, hosting account, '
                    'design files and content on final payment. Register the '
                    'domain in your own name, and confirm you will get admin '
                    'access and a copy of the site so you are never locked in.'
                ),
            },
        ],
    },
    {
        'slug': 'web-design-agency-bristol',
        'keyword': 'web design agency Bristol',
        'title': 'How to Choose a Web Design Agency in Bristol (2026)',
        'meta_description': (
            'How to choose the right web design agency in Bristol in 2026: '
            'the portfolio checks, questions and contract details that '
            'separate a great local agency from an expensive mistake.'
        ),
        'h1': 'How to Choose a Web Design Agency in Bristol',
        'category': 'Hiring',
        'published': '2026-07-06',
        'updated': '2026-07-08',
        'read_time': '9 min read',
        'excerpt': (
            'A practical checklist for choosing a web design agency in '
            'Bristol — what to look for in a portfolio, which questions '
            'reveal the most, and the contract terms that protect you.'
        ),
        'faqs': [
            {
                'q': 'What should I look for in a Bristol web design agency?',
                'a': (
                    'Look for a relevant portfolio, live sites you can '
                    'visit and test on mobile, clear pricing, a defined '
                    'process, and honest talk about SEO and results — not '
                    'just visuals. Local knowledge of the Bristol market is a '
                    'bonus for local-focused businesses.'
                ),
            },
            {
                'q': 'How much does a web design agency in Bristol cost?',
                'a': (
                    'Agency projects in Bristol commonly range from '
                    '£2,500 for a small business site to '
                    '£10,000+ for larger custom or ecommerce builds. '
                    'You are paying for a team, process and accountability, '
                    'which matters most on bigger projects.'
                ),
            },
            {
                'q': 'What questions should I ask before signing?',
                'a': (
                    'Ask who owns the site and domain, what happens if you '
                    'leave, what is included after launch, how long it takes, '
                    'how they measure success, and whether the price is fixed '
                    'or hourly. Get all of it in writing before paying a '
                    'deposit.'
                ),
            },
        ],
    },
]

# Fast lookup by slug for the article route.
ARTICLES_BY_SLUG = {article['slug']: article for article in ARTICLES}
