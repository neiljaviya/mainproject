import os
from flask import Flask, render_template, redirect, url_for, flash, session, request, g, jsonify, Response
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import click
from datetime import datetime, timedelta
import csv
from io import StringIO

from models import db, User, Product, Transaction, TransactionItem, QuickAccessProduct
from forms import (
    LoginForm, ProductForm, TransactionItemForm, PaymentForm,
    ReturnForm, ReturnItemForm, UserForm, QuickAddForm, ProductSearchForm,
    QuickAccessProductForm, CustomProductForm
)

def create_app(test_config=None):
    # Create and configure the app
    app = Flask(__name__, instance_relative_config=True)
    
    # Configure database URI based on environment
    database_url = os.environ.get('DATABASE_URL')
    if database_url and database_url.startswith('postgres://'):
        # Heroku uses postgres:// but SQLAlchemy requires postgresql://
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'dev'),
        SQLALCHEMY_DATABASE_URI=database_url or 'sqlite:///' + os.path.join(app.instance_path, 'pos.sqlite'),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    if test_config is None:
        # Load the instance config, if it exists, when not testing
        app.config.from_pyfile('config.py', silent=True)
    else:
        # Load the test config if passed in
        app.config.from_mapping(test_config)

    # Ensure the instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    # Initialize database
    db.init_app(app)

    # Initialize login manager
    login_manager = LoginManager()
    login_manager.login_view = 'login'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Register CLI commands
    @app.cli.command('init-db')
    def init_db_command():
        """Clear the existing data and create new tables."""
        db.drop_all()
        db.create_all()
        
        # Create default users
        admin = User(
            username='admin',
            password=generate_password_hash('admin123'),
            role='manager'
        )
        cashier = User(
            username='cashier',
            password=generate_password_hash('cashier123'),
            role='cashier'
        )
        
        # Add sample products
        products = [
            Product(name='Milk', price=2.99, quantity=20, category='Dairy'),
            Product(name='Bread', price=1.99, quantity=15, category='Bakery'),
            Product(name='Eggs', price=3.49, quantity=30, category='Dairy'),
            Product(name='Soda', price=1.49, quantity=50, category='Beverages'),
            Product(name='Chips', price=0.99, quantity=40, category='Snacks'),
        ]
        
        db.session.add_all([admin, cashier] + products)
        db.session.commit()
        
        click.echo('Initialized the database.')

    # Authentication routes
    @app.route('/')
    def index():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))
            
        form = LoginForm()
        if form.validate_on_submit():
            user = User.query.filter_by(username=form.username.data).first()
            if user and check_password_hash(user.password, form.password.data):
                login_user(user)
                next_page = request.args.get('next')
                return redirect(next_page or url_for('dashboard'))
            flash('Invalid username or password', 'danger')
        return render_template('login.html', form=form)

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        return redirect(url_for('login'))

    @app.route('/dashboard')
    @login_required
    def dashboard():
        low_stock_products = []
        if current_user.role == 'manager':
            low_stock_products = Product.query.filter(
                Product.quantity <= Product.low_stock_threshold
            ).all()
        return render_template('dashboard.html', low_stock_products=low_stock_products)

    # Product management routes
    @app.route('/products')
    @login_required
    def products():
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        products = Product.query.all()
        return render_template('products.html', products=products)

    @app.route('/products/add', methods=['GET', 'POST'])
    @login_required
    def add_product():
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        form = ProductForm()
        if form.validate_on_submit():
            product = Product(
                name=form.name.data,
                price=form.price.data,
                quantity=form.quantity.data,
                category=form.category.data,
                low_stock_threshold=form.low_stock_threshold.data or 5
            )
            db.session.add(product)
            db.session.commit()
            flash('Product added successfully', 'success')
            return redirect(url_for('products'))
        return render_template('product_form.html', form=form, title='Add Product')

    @app.route('/products/edit/<int:id>', methods=['GET', 'POST'])
    @login_required
    def edit_product(id):
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        product = Product.query.get_or_404(id)
        form = ProductForm(obj=product)
        if form.validate_on_submit():
            product.name = form.name.data
            product.price = form.price.data
            product.quantity = form.quantity.data
            product.category = form.category.data
            product.low_stock_threshold = form.low_stock_threshold.data or 5
            db.session.commit()
            flash('Product updated successfully', 'success')
            return redirect(url_for('products'))
        return render_template('product_form.html', form=form, title='Edit Product')

    @app.route('/products/delete/<int:id>', methods=['POST'])
    @login_required
    def delete_product(id):
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        product = Product.query.get_or_404(id)
        db.session.delete(product)
        db.session.commit()
        flash('Product deleted successfully', 'success')
        return redirect(url_for('products'))

    # Transaction routes
    @app.route('/transactions/new', methods=['GET', 'POST'])
    @login_required
    def new_transaction():
        form = TransactionItemForm()
        form.product_id.choices = [(p.id, f"{p.name} - ${p.price:.2f} ({p.quantity} in stock)") for p in Product.query.filter(Product.quantity > 0).order_by(Product.name).all()]
        
        if form.validate_on_submit():
            product = Product.query.get(form.product_id.data)
            if product:
                cart = session.get('cart', [])
                
                # Get price (use custom price if provided)
                price = form.custom_price.data if form.custom_price.data else product.price
                
                # Check if product already in cart
                for item in cart:
                    if item['product_id'] == product.id:
                        item['quantity'] += form.quantity.data
                        # Update price if custom price is provided
                        if form.custom_price.data:
                            item['price'] = price
                        # Calculate and store subtotal
                        item['subtotal'] = item['price'] * item['quantity']
                        session['cart'] = cart
                        flash(f'Updated quantity for {product.name}', 'success')
                        return redirect(url_for('new_transaction'))
                
                # Add new item to cart with subtotal
                cart.append({
                    'product_id': product.id,
                    'name': product.name,
                    'price': price,
                    'quantity': form.quantity.data,
                    'subtotal': price * form.quantity.data,  # Calculate and store subtotal
                    'is_custom_price': True if form.custom_price.data else False
                })
                session['cart'] = cart
                flash(f'Added {product.name} to cart', 'success')
                return redirect(url_for('new_transaction'))
        
        # Get quick access products
        quick_access_products = []
        for position in range(1, 11):  # Positions 1-10
            quick_product = QuickAccessProduct.query.filter_by(position=position).first()
            if quick_product and quick_product.product.quantity > 0:
                quick_access_products.append({
                    'position': position,
                    'product': quick_product.product
                })
            else:
                quick_access_products.append({
                    'position': position,
                    'product': None
                })
        
        search_form = ProductSearchForm()
        
        # Get last transaction for history display
        last_transaction = None
        if 'last_transaction_id' in session and not cart:
            last_transaction = Transaction.query.get(session['last_transaction_id'])
        
        return render_template('new_transaction.html', form=form, cart=cart, total=total, 
                              search_form=search_form, quick_access_products=quick_access_products,
                              last_transaction=last_transaction)

    @app.route('/transactions/quick_add', methods=['GET', 'POST'])
    @login_required
    def quick_add():
        # Redirect to the new combined product search page
        return redirect(url_for('search_products'))

    @app.route('/update_quantity/<int:product_id>', methods=['POST'])
    @login_required
    def update_quantity(product_id):
        cart = session.get('cart', [])
        quantity = int(request.form.get('quantity', 1))
        
        product = Product.query.get(product_id)
        if not product:
            flash('Product not found', 'danger')
            return redirect(url_for('new_transaction'))
            
        if quantity <= 0:
            # Remove item from cart if quantity is 0 or negative
            cart = [item for item in cart if item['product_id'] != product_id]
            flash('Item removed from cart', 'success')
        else:
            # Check if quantity is available in stock
            if quantity > product.quantity:
                flash(f'Only {product.quantity} available in stock', 'danger')
                return redirect(url_for('new_transaction'))
                
            # Update quantity
            for item in cart:
                if item['product_id'] == product_id:
                    item['quantity'] = quantity
                    flash('Quantity updated', 'success')
                    break
        
        session['cart'] = cart
        return redirect(url_for('new_transaction'))
    
    @app.route('/update_price/<int:product_id>', methods=['POST'])
    @login_required
    def update_price(product_id):
        try:
            new_price = float(request.form.get('price', 0))
            if new_price < 0:
                flash('Price cannot be negative', 'danger')
                return redirect(url_for('new_transaction'))
            
            cart = session.get('cart', [])
            for item in cart:
                if item['product_id'] == product_id:
                    # Store the original price if this is the first price override
                    if not item.get('is_custom_price', False):
                        item['original_price'] = item['price']
                    
                    # Update the price
                    item['price'] = new_price
                    item['is_custom_price'] = True
                    
                    # Recalculate subtotal for this item
                    item['subtotal'] = new_price * item['quantity']
                    
                    session['cart'] = cart
                    session.modified = True  # Force session update
                    
                    flash('Price updated', 'success')
                    break
                    
            return redirect(url_for('new_transaction'))
        except ValueError:
            flash('Invalid price value', 'danger')
            return redirect(url_for('new_transaction'))

    @app.route('/transactions/clear_cart', methods=['GET'])
    @login_required
    def confirm_clear_cart():
        return render_template('confirm_clear_cart.html')

    @app.route('/transactions/clear_cart', methods=['POST'])
    @login_required
    def clear_cart():
        session.pop('cart', None)
        flash('Cart cleared', 'success')
        return redirect(url_for('new_transaction'))

    @app.route('/products/search', methods=['GET', 'POST'])
    @login_required
    def search_products():
        form = ProductSearchForm()
        quick_add_form = QuickAddForm()
        products = []
        
        # Handle product search form submission
        if form.validate_on_submit() or request.args.get('search_term'):
            search_term = form.search_term.data or request.args.get('search_term', '')
            products = Product.query.filter(
                (Product.name.ilike(f'%{search_term}%')) | 
                (Product.sku.ilike(f'%{search_term}%')) | 
                (Product.barcode.ilike(f'%{search_term}%')) | 
                (Product.category.ilike(f'%{search_term}%'))
            ).all()
        
        # Handle quick add form submission
        if quick_add_form.validate_on_submit():
            product_code = quick_add_form.product_code.data
            quantity = quick_add_form.quantity.data
            
            product = Product.query.filter((Product.barcode == product_code) | (Product.sku == product_code)).first()
            
            if product:
                cart = session.get('cart', [])
                
                # Check if product already in cart
                for item in cart:
                    if item['product_id'] == product.id:
                        item['quantity'] += quantity
                        session['cart'] = cart
                        flash(f'Updated quantity for {product.name}', 'success')
                        return redirect(url_for('search_products'))
                
                cart.append({
                    'product_id': product.id,
                    'name': product.name,
                    'price': product.price,
                    'quantity': quantity
                })
                session['cart'] = cart
                flash(f'Added {product.name} to cart', 'success')
                return redirect(url_for('search_products'))
        
        cart = session.get('cart', [])
        total = sum(item['price'] * item['quantity'] for item in cart)
            
        return render_template('product_search.html', form=form, quick_add_form=quick_add_form, products=products, cart=cart, total=total)

    @app.route('/api/products/search', methods=['GET'])
    @login_required
    def api_search_products():
        search_term = request.args.get('term', '')
        if len(search_term) < 2:
            return jsonify([])
            
        products = Product.query.filter(
            (Product.name.ilike(f'%{search_term}%')) | 
            (Product.sku.ilike(f'%{search_term}%')) | 
            (Product.barcode.ilike(f'%{search_term}%')) | 
            (Product.category.ilike(f'%{search_term}%'))
        ).limit(10).all()
        
        results = [{'id': p.id, 'text': f"{p.name} - ${p.price:.2f} ({p.quantity} in stock)"} for p in products]
        return jsonify(results)

    @app.route('/products/add_to_cart/<int:product_id>', methods=['POST'])
    @login_required
    def add_to_cart_from_search(product_id):
        product = Product.query.get_or_404(product_id)
        
        if product.quantity <= 0:
            flash('Product is out of stock', 'danger')
            return redirect(url_for('search_products'))
            
        cart = session.get('cart', [])
        
        # Check if product already in cart
        for item in cart:
            if item['product_id'] == product.id:
                item['quantity'] += 1
                session['cart'] = cart
                flash(f'Updated quantity for {product.name}', 'success')
                return redirect(url_for('search_products'))
        
        cart.append({
            'product_id': product.id,
            'name': product.name,
            'price': product.price,
            'quantity': 1
        })
        session['cart'] = cart
        flash(f'Added {product.name} to cart', 'success')
        return redirect(url_for('search_products'))

    @app.route('/transactions/remove/<int:product_id>', methods=['POST'])
    @login_required
    def remove_from_cart(product_id):
        cart = session.get('cart', [])
        cart = [item for item in cart if item['product_id'] != product_id]
        session['cart'] = cart
        flash('Item removed from cart', 'success')
        return redirect(url_for('new_transaction'))

    @app.route('/transactions/checkout', methods=['GET', 'POST'])
    @login_required
    def checkout():
        cart = session.get('cart', [])
        if not cart:
            flash('Your cart is empty. Add products before checkout.', 'warning')
            return redirect(url_for('new_transaction'))
        
        form = PaymentForm()
        
        if form.validate_on_submit():
            payment_method = form.payment_method.data
            amount_tendered = form.amount_tendered.data
            discount_amount = form.discount_amount.data or 0
            
            # Calculate total - using the UPDATED prices in the cart
            subtotal = sum(item['price'] * item['quantity'] for item in cart)
            total_amount = subtotal - discount_amount
            
            if amount_tendered < total_amount:
                flash('Amount tendered must be at least equal to the total amount.', 'danger')
                return render_template('checkout.html', cart=cart, form=form, subtotal=subtotal, total=total_amount)
            
            # Create transaction
            transaction = Transaction(
                total_amount=total_amount,
                payment_method=payment_method,
                user_id=current_user.id,
                discount_amount=discount_amount
            )
            db.session.add(transaction)
            db.session.flush()  # Get transaction ID without committing
            
            # Add transaction items with proper prices
            for item in cart:
                if item.get('is_custom_product', False):
                    # Custom product (not in inventory)
                    transaction_item = TransactionItem(
                        transaction_id=transaction.id,
                        quantity=item['quantity'],
                        price_at_time_of_sale=item['price'],  # Use current price from cart
                        custom_name=item['name'],
                        is_custom_product=True
                    )
                    db.session.add(transaction_item)
                else:
                    # Regular product
                    product_id = item['product_id']
                    product = Product.query.get(product_id)
                    
                    if product:
                        # Update inventory
                        if product.quantity >= item['quantity']:
                            product.quantity -= item['quantity']
                            
                            # Create transaction item with the price from cart (which may be custom)
                            transaction_item = TransactionItem(
                                transaction_id=transaction.id,
                                product_id=product_id,
                                quantity=item['quantity'],
                                price_at_time_of_sale=item['price'],  # Use current price from cart
                                is_custom_product=False
                            )
                            db.session.add(transaction_item)
                        else:
                            db.session.rollback()
                            flash(f'Not enough stock for {product.name}. Only {product.quantity} available.', 'danger')
                            return render_template('checkout.html', cart=cart, form=form, subtotal=subtotal, total=total_amount)
            
            # Commit transaction
            db.session.commit()
            
            # Store the transaction ID in the session for history display
            session['last_transaction_id'] = transaction.id
            
            # Clear cart
            session.pop('cart', None)
            
            # Calculate change
            change = amount_tendered - total_amount
            
            return render_template('receipt.html', 
                                  transaction=transaction, 
                                  payment_method=payment_method,
                                  amount_tendered=amount_tendered,
                                  change=change)
        
        # Calculate totals using current prices in cart
        subtotal = sum(item['price'] * item['quantity'] for item in cart)
        
        return render_template('checkout.html', cart=cart, form=form, subtotal=subtotal, total=subtotal)

    # Returns routes
    @app.route('/returns', methods=['GET', 'POST'])
    @login_required
    def returns():
        form = ReturnForm()
        if form.validate_on_submit():
            transaction = Transaction.query.get(form.transaction_id.data)
            if not transaction:
                flash('Transaction not found', 'danger')
                return redirect(url_for('returns'))
            return redirect(url_for('process_return', transaction_id=transaction.id))
        return render_template('returns.html', form=form)

    @app.route('/returns/<int:transaction_id>', methods=['GET', 'POST'])
    @login_required
    def process_return(transaction_id):
        transaction = Transaction.query.get_or_404(transaction_id)
        
        if request.method == 'POST':
            # Process return items
            return_items = []
            total_return_amount = 0
            
            for item in transaction.items:
                return_quantity = int(request.form.get(f'return_quantity_{item.id}', 0))
                if return_quantity > 0:
                    if return_quantity > item.quantity:
                        flash(f'Cannot return more than purchased quantity for {item.product.name}', 'danger')
                        return redirect(url_for('process_return', transaction_id=transaction.id))
                    
                    return_items.append({
                        'item': item,
                        'return_quantity': return_quantity,
                        'return_amount': return_quantity * item.price_at_time_of_sale
                    })
                    total_return_amount += return_quantity * item.price_at_time_of_sale
            
            if not return_items:
                flash('No items selected for return', 'danger')
                return redirect(url_for('process_return', transaction_id=transaction.id))
            
            # Create return transaction
            return_transaction = Transaction(
                total_amount=total_return_amount,
                payment_method=transaction.payment_method,
                user_id=current_user.id,
                is_return=True
            )
            db.session.add(return_transaction)
            
            # Create return items and update inventory
            for return_item in return_items:
                item = return_item['item']
                return_quantity = return_item['return_quantity']
                
                transaction_item = TransactionItem(
                    transaction=return_transaction,
                    product_id=item.product_id,
                    quantity=return_quantity,
                    price_at_time_of_sale=item.price_at_time_of_sale
                )
                db.session.add(transaction_item)
                
                # Update inventory
                product = Product.query.get(item.product_id)
                product.quantity += return_quantity
            
            db.session.commit()
            
            flash('Return processed successfully', 'success')
            return redirect(url_for('dashboard'))
        
        return render_template('process_return.html', transaction=transaction)

    # Reporting routes
    @app.route('/reports')
    @login_required
    def reports():
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
        return render_template('reports.html')

    @app.route('/reports/sales')
    @login_required
    def sales_report():
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        # Get period from query parameter, default to 'all'
        period = request.args.get('period', 'all')
        
        # Get transactions based on the selected period
        transactions, start_date, end_date = get_transactions_by_period(period)
        
        # Calculate totals
        total_sales = sum(t.total_amount for t in transactions)
        avg_transaction = total_sales / len(transactions) if transactions else 0
        
        return render_template(
            'sales_report.html', 
            transactions=transactions, 
            total_sales=total_sales,
            avg_transaction=avg_transaction,
            period=period,
            start_date=start_date,
            end_date=end_date
        )
    
    @app.route('/api/reports/sales')
    @login_required
    def api_sales_report():
        if current_user.role != 'manager':
            return jsonify({'error': 'Access denied'}), 403
            
        period = request.args.get('period', 'all')
        transactions, start_date, end_date = get_transactions_by_period(period)
        
        # Format data for API response
        transaction_data = [{
            'id': t.id,
            'date': t.date.strftime('%Y-%m-%d %H:%M:%S'),
            'cashier': t.user.username,
            'items_count': len(t.items),
            'payment_method': t.payment_method,
            'discount': t.discount_amount,
            'total': t.total_amount
        } for t in transactions]
        
        total_sales = sum(t.total_amount for t in transactions)
        avg_transaction = total_sales / len(transactions) if transactions else 0
        
        return jsonify({
            'transactions': transaction_data,
            'total_sales': total_sales,
            'transaction_count': len(transactions),
            'avg_transaction': avg_transaction,
            'period': period,
            'start_date': start_date.strftime('%Y-%m-%d') if start_date else None,
            'end_date': end_date.strftime('%Y-%m-%d') if end_date else None
        })
    
    @app.route('/api/reports/sales/chart-data')
    @login_required
    def api_sales_chart_data():
        if current_user.role != 'manager':
            return jsonify({'error': 'Access denied'}), 403
            
        period = request.args.get('period', 'all')
        transactions, start_date, end_date = get_transactions_by_period(period)
        
        # Group data by date for charting
        chart_data = {}
        
        if period == 'daily':
            # Group by hour
            for t in transactions:
                hour = t.date.strftime('%H:00')
                if hour not in chart_data:
                    chart_data[hour] = 0
                chart_data[hour] += t.total_amount
                
        elif period == 'weekly':
            # Group by day of week
            for t in transactions:
                day = t.date.strftime('%a')  # Mon, Tue, etc.
                if day not in chart_data:
                    chart_data[day] = 0
                chart_data[day] += t.total_amount
                
        elif period == 'monthly':
            # Group by day of month
            for t in transactions:
                day = t.date.strftime('%d')  # 01, 02, etc.
                if day not in chart_data:
                    chart_data[day] = 0
                chart_data[day] += t.total_amount
                
        elif period == 'yearly':
            # Group by month
            for t in transactions:
                month = t.date.strftime('%b')  # Jan, Feb, etc.
                if month not in chart_data:
                    chart_data[month] = 0
                chart_data[month] += t.total_amount
                
        else:
            # Group by month-year for all time or custom
            for t in transactions:
                month_year = t.date.strftime('%b %Y')
                if month_year not in chart_data:
                    chart_data[month_year] = 0
                chart_data[month_year] += t.total_amount
        
        # Convert to lists for Chart.js
        labels = list(chart_data.keys())
        values = list(chart_data.values())
        
        return jsonify({
            'labels': labels,
            'values': values
        })
    
    def get_transactions_by_period(period):
        """
        Get transactions filtered by time period.
        
        Args:
            period (str): One of 'daily', 'weekly', 'monthly', 'yearly', 'all', 
                          or 'custom:YYYY-MM-DD:YYYY-MM-DD' for custom date range
        
        Returns:
            tuple: (transactions, start_date, end_date)
        """
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = None
        end_date = None
        
        if period == 'daily':
            # Today's transactions
            start_date = today
            end_date = today + timedelta(days=1) - timedelta(microseconds=1)
        elif period == 'weekly':
            # This week's transactions (starting Monday)
            start_date = today - timedelta(days=today.weekday())
            end_date = start_date + timedelta(days=7) - timedelta(microseconds=1)
        elif period == 'monthly':
            # This month's transactions
            start_date = today.replace(day=1)
            if start_date.month == 12:
                end_date = start_date.replace(year=start_date.year + 1, month=1) - timedelta(microseconds=1)
            else:
                end_date = start_date.replace(month=start_date.month + 1) - timedelta(microseconds=1)
        elif period == 'yearly':
            # This year's transactions
            start_date = today.replace(month=1, day=1)
            end_date = start_date.replace(year=start_date.year + 1) - timedelta(microseconds=1)
        elif period.startswith('custom:'):
            # Custom date range: custom:YYYY-MM-DD:YYYY-MM-DD
            try:
                dates = period.split(':')
                start_date = datetime.strptime(dates[1], '%Y-%m-%d')
                end_date = datetime.strptime(dates[2], '%Y-%m-%d') + timedelta(days=1) - timedelta(microseconds=1)
            except (IndexError, ValueError):
                # If custom format is invalid, default to all transactions
                pass
        
        # Filter transactions based on date range
        if start_date and end_date:
            transactions = Transaction.query.filter(
                Transaction.date >= start_date,
                Transaction.date <= end_date,
                Transaction.is_return == False
            ).order_by(Transaction.date.desc()).all()
        else:
            # Default: all transactions
            transactions = Transaction.query.filter_by(is_return=False).order_by(Transaction.date.desc()).all()
            
        return transactions, start_date, end_date
    
    @app.route('/reports/inventory')
    @login_required
    def inventory_report():
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        products = Product.query.all()
        return render_template('inventory_report.html', products=products)

    # User management routes
    @app.route('/users')
    @login_required
    def users():
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        users = User.query.all()
        return render_template('users.html', users=users)

    @app.route('/users/add', methods=['GET', 'POST'])
    @login_required
    def add_user():
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        form = UserForm()
        if form.validate_on_submit():
            # Check if username already exists
            existing_user = User.query.filter_by(username=form.username.data).first()
            if existing_user:
                flash('Username already exists', 'danger')
                return render_template('user_form.html', form=form, title='Add User')
                
            user = User(
                username=form.username.data,
                password=generate_password_hash(form.password.data),
                role=form.role.data
            )
            db.session.add(user)
            db.session.commit()
            flash('User added successfully', 'success')
            return redirect(url_for('users'))
        return render_template('user_form.html', form=form, title='Add User')

    @app.route('/users/edit/<int:id>', methods=['GET', 'POST'])
    @login_required
    def edit_user(id):
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        user = User.query.get_or_404(id)
        form = UserForm(obj=user)
        
        # Don't require password for edit
        if request.method == 'GET':
            form.password.data = ''
            
        if form.validate_on_submit():
            # Check if username already exists and is not the current user
            existing_user = User.query.filter_by(username=form.username.data).first()
            if existing_user and existing_user.id != id:
                flash('Username already exists', 'danger')
                return render_template('user_form.html', form=form, title='Edit User')
                
            user.username = form.username.data
            if form.password.data:
                user.password = generate_password_hash(form.password.data)
            user.role = form.role.data
            db.session.commit()
            flash('User updated successfully', 'success')
            return redirect(url_for('users'))
        return render_template('user_form.html', form=form, title='Edit User')

    @app.route('/users/delete/<int:id>', methods=['POST'])
    @login_required
    def delete_user(id):
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        if id == current_user.id:
            flash('Cannot delete your own account', 'danger')
            return redirect(url_for('users'))
            
        user = User.query.get_or_404(id)
        db.session.delete(user)
        db.session.commit()
        flash('User deleted successfully', 'success')
        return redirect(url_for('users'))

    @app.route('/quick_access/manage', methods=['GET'])
    @login_required
    def manage_quick_access():
        if current_user.role != 'manager':
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('dashboard'))
            
        quick_access_products = []
        for position in range(1, 11):  # Positions 1-10
            quick_product = QuickAccessProduct.query.filter_by(position=position).first()
            if quick_product:
                quick_access_products.append({
                    'position': position,
                    'product_id': quick_product.product_id,
                    'product_name': quick_product.product.name
                })
            else:
                quick_access_products.append({
                    'position': position,
                    'product_id': None,
                    'product_name': 'Not Set'
                })
                
        form = QuickAccessProductForm()
        form.product_id.choices = [(p.id, p.name) for p in Product.query.order_by(Product.name).all()]
        
        return render_template('manage_quick_access.html', quick_access_products=quick_access_products, form=form)
        
    @app.route('/quick_access/update', methods=['POST'])
    @login_required
    def update_quick_access():
        if current_user.role != 'manager':
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('dashboard'))
            
        form = QuickAccessProductForm()
        form.product_id.choices = [(p.id, p.name) for p in Product.query.order_by(Product.name).all()]
        
        if form.validate_on_submit():
            position = int(form.position.data)
            product_id = form.product_id.data
            
            # Check if position already exists
            existing = QuickAccessProduct.query.filter_by(position=position).first()
            if existing:
                existing.product_id = product_id
                db.session.commit()
                flash(f'Updated quick access button at position {position}', 'success')
            else:
                new_quick_access = QuickAccessProduct(position=position, product_id=product_id)
                db.session.add(new_quick_access)
                db.session.commit()
                flash(f'Added quick access button at position {position}', 'success')
                
        return redirect(url_for('manage_quick_access'))
        
    @app.route('/quick_access/add_to_cart/<int:product_id>', methods=['POST'])
    @login_required
    def add_to_cart_from_quick_access(product_id):
        product = Product.query.get_or_404(product_id)
        
        if product.quantity <= 0:
            flash(f'Sorry, {product.name} is out of stock.', 'danger')
            return redirect(url_for('new_transaction'))
            
        cart = session.get('cart', [])
        
        # Check if product already in cart
        for item in cart:
            if item['product_id'] == product.id:
                item['quantity'] += 1  # Add one by default
                # Calculate and store subtotal
                item['subtotal'] = item['price'] * item['quantity']
                session['cart'] = cart
                flash(f'Updated quantity for {product.name}', 'success')
                return redirect(url_for('new_transaction'))
        
        # Add new item to cart with subtotal
        cart.append({
            'product_id': product.id,
            'name': product.name,
            'price': product.price,
            'quantity': 1,  # Add one by default
            'subtotal': product.price * 1  # Calculate and store subtotal
        })
        session['cart'] = cart
        flash(f'Added {product.name} to cart', 'success')
        return redirect(url_for('new_transaction'))

    @app.route('/transactions/custom_product', methods=['GET', 'POST'])
    @login_required
    def add_custom_product():
        form = CustomProductForm()
        
        if form.validate_on_submit():
            name = form.name.data
            price = form.price.data
            quantity = form.quantity.data
            
            cart = session.get('cart', [])
            
            # Generate a unique ID for the custom product
            custom_id = f"custom_{len(cart)}"
            
            cart.append({
                'product_id': custom_id,
                'name': name,
                'price': price,
                'quantity': quantity,
                'is_custom_product': True
            })
            
            session['cart'] = cart
            flash(f'Added custom product: {name} to cart', 'success')
            return redirect(url_for('new_transaction'))
        
        return render_template('custom_product.html', form=form)

    @app.route('/reports/sales/export')
    @login_required
    def export_sales_report():
        if current_user.role != 'manager':
            flash('Access denied. Manager role required.', 'danger')
            return redirect(url_for('dashboard'))
            
        period = request.args.get('period', 'all')
        transactions, start_date, end_date = get_transactions_by_period(period)
        
        # Create CSV data
        output = StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow(['ID', 'Date', 'Cashier', 'Items', 'Payment Method', 'Discount', 'Total'])
        
        # Write transaction data
        for t in transactions:
            writer.writerow([
                t.id,
                t.date.strftime('%Y-%m-%d %H:%M:%S'),
                t.user.username,
                len(t.items),
                t.payment_method,
                f"${t.discount_amount:.2f}" if t.discount_amount > 0 else "-",
                f"${t.total_amount:.2f}"
            ])
        
        # Prepare response
        output.seek(0)
        
        # Generate filename based on period
        if period == 'daily':
            filename = f"sales_report_daily_{start_date.strftime('%Y-%m-%d')}.csv"
        elif period == 'weekly':
            filename = f"sales_report_weekly_{start_date.strftime('%Y-%m-%d')}_to_{end_date.strftime('%Y-%m-%d')}.csv"
        elif period == 'monthly':
            filename = f"sales_report_monthly_{start_date.strftime('%Y-%m')}.csv"
        elif period == 'yearly':
            filename = f"sales_report_yearly_{start_date.strftime('%Y')}.csv"
        elif period.startswith('custom:'):
            filename = f"sales_report_custom_{start_date.strftime('%Y-%m-%d')}_to_{end_date.strftime('%Y-%m-%d')}.csv"
        else:
            filename = "sales_report_all.csv"
        
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename={filename}"}
        )

    # Add custom Jinja2 filters
    @app.template_filter('count_low_stock')
    def count_low_stock(products):
        """Count products where quantity is less than or equal to low_stock_threshold"""
        return sum(1 for p in products if p.quantity <= p.low_stock_threshold)

    return app

app = create_app()

if __name__ == '__main__':
    app.run(debug=True) 
